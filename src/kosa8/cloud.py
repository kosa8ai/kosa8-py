"""kosa8 Cloud: the same sandboxes, run on kosa8's hosts (or your own, with
bring-your-own-cloud) instead of the local daemon.

>>> from kosa8 import Kosa8Cloud
>>> cloud = Kosa8Cloud()  # KOSA8_API_KEY from the environment
>>> box = cloud.sandboxes.create(template="python", ttl_s=3600)
>>> box.exec(["python3", "-c", "print(6 * 7)"]).stdout
'42\\n'
>>> box.delete()

Authentication is an API key (``k8c_live_...``) made in the cloud dashboard.
The address is ``https://api.kosa8.com`` (``KOSA8_CLOUD_URL`` overrides it).
Errors are the SDK's usual typed errors; each carries the cloud's stable
``code`` (for example ``payment_required``, ``quota_exceeded``).

How the cloud runs commands: each ``exec`` runs in a fresh container, and only
``/workspace`` persists between them. The ``base`` template is Alpine Linux
(busybox ``sh``; no bash, python or git). A buffered exec is cut off after
about 30 seconds (:class:`~kosa8.Kosa8ExecTimeoutError`), and a request body
is at most 64 KiB.

Standard library only, like the rest of the SDK.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from .errors import (
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
    error_from_response,
)
from .cloud_features import (
    CloudBackgroundExec,
    CloudCodeContexts,
    CloudPipelines,
    CloudPreviews,
    CloudSchedules,
    CloudSecrets,
    CloudSnapshots,
    argv_of,
    env_list,
)

DEFAULT_CLOUD_URL = "https://api.kosa8.com"
CLOUD_KEY_PREFIX = "k8c_live_"
#: Sent with every Cloud request: the API's edge refuses Python-urllib's
#: default User-Agent (Cloudflare error 1010, a bare 403).
USER_AGENT = "kosa8-python/0.4.0"

#: A ttl_s is from 60 seconds to 90 days.
MIN_TTL_S = 60
MAX_TTL_S = 7776000
#: sleep_after_s is from 0 (never) to 7 days.
MAX_SLEEP_AFTER_S = 604800
#: A fork makes from 1 to 100 copies.
MAX_FORK = 100
#: The limit kosa8 Cloud puts on a buffered exec today, in seconds.
EXEC_LIMIT_S = 30
#: vCPU x GiB sizes a sized create takes.
CLOUD_SIZES = ("2x4", "4x8", "8x16", "16x32")
#: States a booting sandbox does not leave on its own.
_SETTLED = frozenset({"running", "error", "deleted", "stopped", "host_removed", "host_lost", "sleeping", "paused"})

#: (method, url, headers, body bytes or None, timeout) -> (status, body bytes),
#: or (status, body bytes, response headers).
Opener = Callable[[str, str, Dict[str, str], Optional[bytes], float], Any]

_EXEC_HINT = (
    f"a buffered exec is limited to about {EXEC_LIMIT_S} s; run long work in the background, "
    "writing to /workspace, and poll it"
)


def _check_base(url: str) -> str:
    u = urllib.parse.urlparse(url)
    if not u.scheme or not u.netloc:
        raise Kosa8Error(f"kosa8 Cloud address is not a URL: {url}")
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        # The API key travels in every request.
        raise Kosa8Error(f"kosa8 Cloud address must use https: {url}")
    return url.rstrip("/")


def _urllib_open(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https checked above)
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers.items()) if e.headers else {}


@dataclass
class CloudExecResult:
    #: ``None`` when the command did not report an exit code.
    exit_code: Optional[int]
    stdout: str
    stderr: str
    #: Set when the sandbox was asleep and woke for this command.
    woke_ms: Optional[int] = None


@dataclass
class CloudSandboxPage:
    """One page of :meth:`CloudSandboxes.list_page`.

    ``next_cursor`` is ``None`` on the last page, and always on a server that
    does not page yet (it then answers every sandbox in one page).
    """

    sandboxes: List["CloudSandbox"]
    next_cursor: Optional[str] = None


@dataclass
class CloudRewindResult:
    #: The checkpoint it went back to.
    checkpoint: Dict[str, Any]
    #: The automatic checkpoint of the sandbox as it was just before the rewind.
    backup: Optional[Dict[str, Any]] = None
    ms: Optional[int] = None


class Kosa8Cloud:
    """A client for the kosa8 Cloud control plane."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = 120.0,
        opener: Optional[Opener] = None,
    ) -> None:
        env = os.environ if env is None else env
        key = (api_key if api_key is not None else env.get("KOSA8_API_KEY", "")).strip()
        if not key:
            raise Kosa8Error("no kosa8 Cloud API key: pass api_key or set KOSA8_API_KEY")
        if not key.startswith(CLOUD_KEY_PREFIX):
            raise Kosa8Error(f"a kosa8 Cloud API key starts with {CLOUD_KEY_PREFIX}")
        self._key = key
        self.base_url = _check_base(base_url or env.get("KOSA8_CLOUD_URL") or DEFAULT_CLOUD_URL)
        self.timeout = timeout
        self._open = opener or _urllib_open
        self.sandboxes = CloudSandboxes(self)
        self.functions = CloudFunctions(self)
        #: Durable snapshots.
        self.snapshots = CloudSnapshots(self)
        #: Org secrets (values go in, never come out).
        self.secrets = CloudSecrets(self)
        #: CI runs, logs, artifacts and triggers.
        self.pipelines = CloudPipelines(self)

    def __repr__(self) -> str:  # never the key
        return f"Kosa8Cloud(base_url={self.base_url!r})"

    def me(self) -> Dict[str, Any]:
        """The org, the key in use and, for a member's key, the member."""
        return self._request("GET", "/v1/me")

    def usage(self) -> Dict[str, Any]:
        """This month's usage, bill estimate and whether work is paused for payment."""
        return self._request("GET", "/v1/usage")

    def templates(self) -> List[Dict[str, Any]]:
        """The templates the org can start: ``key``, ``cpus``, ``mem_mb``, ``image``, ``regions``, ``warm``."""
        return (self._request("GET", "/v1/templates") or {}).get("templates", [])

    def capabilities(self) -> Dict[str, Any]:
        """What this API offers, for feature detection."""
        return self._request("GET", "/v1/capabilities") or {}

    def egress_presets(self) -> Dict[str, Any]:
        """Named egress presets (e.g. ``pypi``, ``npm``) and the hosts each allows."""
        return (self._request("GET", "/v1/egress/presets") or {}).get("presets", {})

    def org_egress(self) -> Optional[Dict[str, Any]]:
        """The org's default egress policy (``default``, ``enforce``), or ``None`` (open internet)."""
        return (self._request("GET", "/v1/org/egress") or {}).get("egress")

    def set_org_egress(self, policy: Optional[Mapping[str, Any]], *, enforce: Optional[bool] = None) -> Dict[str, Any]:
        """Set the org's default egress policy (an admin key); ``None`` clears it.
        It is pushed at once to the running and sleeping sandboxes it changes;
        with ``enforce`` it is a ceiling sandboxes cannot widen."""
        body: Dict[str, Any] = {"default": dict(policy) if policy is not None else None}
        if policy is not None and enforce is not None:
            body["enforce"] = enforce
        return self._request("PUT", "/v1/org/egress", body)

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        data: Optional[bytes] = None,
        raw: bool = False,
    ) -> Any:
        """One call. ``body`` is sent as JSON and ``data`` as raw bytes; with
        ``raw`` a successful answer's bytes are returned as they came."""
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/octet-stream" if raw else "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
            payload = data
        elif body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        try:
            answer = self._open(method, self.base_url + path, headers, payload, self.timeout)
        except (socket.timeout, TimeoutError) as e:
            if _is_exec_path(path):
                raise Kosa8ExecTimeoutError(
                    f"kosa8 Cloud did not answer within {self.timeout} s ({_EXEC_HINT})",
                    method=method, path=path, code="exec_timeout",
                ) from e
            raise Kosa8TimeoutError(f"kosa8 Cloud did not answer within {self.timeout} s", method=method, path=path) from e
        except (urllib.error.URLError, OSError) as e:
            raise Kosa8ConnectionError(f"could not reach kosa8 Cloud at {self.base_url}: {e}", method=method, path=path) from e
        status, content = answer[0], answer[1]
        resp_headers = answer[2] if len(answer) > 2 and answer[2] else {}
        if raw and status < 400:
            return content or b""
        text = content.decode("utf-8", "replace") if content else ""
        try:
            parsed: Any = json.loads(text) if text else None
        except ValueError:
            parsed = text
        if status >= 400:
            raise _cloud_error(status, parsed, method, path, resp_headers)
        return parsed


def _is_exec_path(path: str) -> bool:
    return path.split("?", 1)[0].endswith("/exec")


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _is_exec_timeout(status: int, code: Optional[str], message: str, path: str) -> bool:
    if status == 504 or code == "exec_timeout":
        return True
    # Today the Worker gives up on the host after about 30 s and answers
    # 502 host_failed "exec failed on the host: ... timed out".
    return status == 502 and _is_exec_path(path) and "timed out" in message.lower()


def _cloud_error(
    status: int, body: Any, method: str, path: str, headers: Optional[Mapping[str, str]] = None
) -> Kosa8Error:
    # The cloud answers {error: code, message}: the sentence is the message,
    # and the code is kept.
    code = None
    msg_body = body
    if isinstance(body, dict):
        if isinstance(body.get("error"), str):
            code = body["error"]
        msg_body = {"error": body.get("message") or body.get("error")}
    if isinstance(msg_body, dict):
        message = str(msg_body.get("error") or "")
    else:
        message = str(msg_body or "")
    e: Kosa8Error
    if _is_exec_timeout(status, code, message, path):
        e = Kosa8ExecTimeoutError(
            f"{message or f'request failed with HTTP {status}'} ({_EXEC_HINT})",
            status=status, daemon_message=message or None, method=method, path=path, code=code or "exec_timeout",
        )
    else:
        e = error_from_response(status, msg_body, method=method, path=path, code=code)
    e.body = body
    if isinstance(e, Kosa8RateLimitError) and headers:
        ra = _header(headers, "Retry-After")
        try:
            e.retry_after = float(ra) if ra is not None else None
        except ValueError:
            e.retry_after = None
    return e


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_lifetime(ttl_s: Optional[int], delete_after: Optional[int]) -> None:
    if ttl_s is not None and delete_after is not None:
        raise ValueError("give ttl_s or delete_after, not both")
    if ttl_s is not None and (not _is_int(ttl_s) or not MIN_TTL_S <= ttl_s <= MAX_TTL_S):
        raise ValueError(f"ttl_s must be a whole number of seconds from {MIN_TTL_S} to {MAX_TTL_S}")
    if delete_after is not None and not _is_int(delete_after):
        raise ValueError("delete_after must be a Unix time in milliseconds")


def _check_sleep_after(sleep_after_s: Optional[int]) -> None:
    if sleep_after_s is not None and (not _is_int(sleep_after_s) or not 0 <= sleep_after_s <= MAX_SLEEP_AFTER_S):
        raise ValueError(f"sleep_after_s must be a whole number of seconds from 0 (never) to {MAX_SLEEP_AFTER_S}")


class CloudSandboxes:
    def __init__(self, cloud: Kosa8Cloud) -> None:
        self._cloud = cloud

    def create(
        self,
        *,
        template: Optional[str] = None,
        region: Optional[str] = None,
        sleep_after_s: Optional[int] = None,
        ttl_s: Optional[int] = None,
        delete_after: Optional[int] = None,
        accept_price: Optional[float] = None,
        size: Optional[str] = None,
        cpus: Optional[int] = None,
        mem_mb: Optional[int] = None,
        image: Optional[str] = None,
        disk_gb: Optional[int] = None,
        persistent: Optional[bool] = None,
        docker: Optional[bool] = None,
        cpu: Optional[str] = None,
        name: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        snapshot: Optional[Union[str, Mapping[str, Any]]] = None,
        egress: Optional[Mapping[str, Any]] = None,
        secrets: Optional[Sequence[str]] = None,
        labels: Optional[Mapping[str, str]] = None,
        wait: bool = True,
        wait_timeout: float = 300.0,
    ) -> "CloudSandbox":
        """Start a sandbox; returns once it is running.

        Sized and Docker sandboxes: ``size`` (``2x4``, ``4x8``, ``8x16`` or
        ``16x32``) or ``cpus`` + ``mem_mb``, ``image`` (an OCI image such as
        ``kosa8-template/universal-24.04-20261115.2``), ``disk_gb`` (8 to 200)
        boot cold at that size (the API answers ``creating``; this polls until
        it is running unless ``wait=False``). ``docker=True`` runs a Docker
        engine in it and makes it ``persistent`` (one long-lived container).
        ``cpu="dedicated"`` gives whole physical cores. ``env`` is given to
        every command (values are never logged or returned). ``snapshot``
        (``snap_...`` or a snapshot dict) starts it as the snapshot was; it
        then takes no template, size, image, disk, docker, persistent, cpu or
        region. ``egress`` is its starting network policy, ``secrets`` names
        org secrets it holds, ``labels`` are set right after it is created.

        ``template`` is a key from :meth:`Kosa8Cloud.templates` (default the
        org's default, ``base``: Alpine, busybox sh, no bash or python) or a
        kosa8 Store item, ``creator/name`` or ``creator/name@1.2.3``.
        ``ttl_s`` (60 s to 90 days) or ``delete_after`` (Unix ms) deletes it
        automatically: set one so a crashed script cannot leave it running.
        ``sleep_after_s`` is the idle time before it sleeps (0 never; default
        300). ``accept_price`` is a paid Store item's current price in USD,
        which the person agreed to.
        """
        _check_lifetime(ttl_s, delete_after)
        _check_sleep_after(sleep_after_s)
        body: Dict[str, Any] = {}
        if template is not None:
            body["template"] = template
        if region is not None:
            body["region"] = region
        if sleep_after_s is not None:
            body["sleep_after_s"] = sleep_after_s
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        if delete_after is not None:
            body["delete_after"] = delete_after
        if accept_price is not None:
            body["accept_price"] = accept_price
        if snapshot is not None:
            clash = [k for k, v in (
                ("template", template), ("size", size), ("cpus", cpus), ("mem_mb", mem_mb), ("image", image),
                ("disk_gb", disk_gb), ("docker", docker), ("persistent", persistent), ("cpu", cpu), ("region", region),
            ) if v is not None]
            if clash:
                raise ValueError(
                    "a sandbox made from a snapshot takes its settings from the snapshot; leave out " + ", ".join(clash)
                )
            body["snapshot"] = snapshot if isinstance(snapshot, str) else snapshot["id"]
        if (cpus is None) != (mem_mb is None):
            raise ValueError("give cpus and mem_mb together")
        if size is not None and cpus is not None:
            raise ValueError("give size or cpus + mem_mb, not both")
        if disk_gb is not None and (not _is_int(disk_gb) or not 8 <= disk_gb <= 200):
            raise ValueError("disk_gb must be a whole number from 8 to 200")
        if docker and persistent is False:
            raise ValueError("a sandbox with docker is persistent; leave out persistent or set it to True")
        for k, v in (
            ("size", size), ("cpus", cpus), ("mem_mb", mem_mb), ("image", image), ("disk_gb", disk_gb),
            ("docker", docker), ("persistent", persistent), ("cpu", cpu), ("name", name),
        ):
            if v is not None:
                body[k] = v
        if env is not None:
            body["env"] = dict(env)
        if egress is not None:
            body["egress"] = dict(egress)
        if secrets is not None:
            body["secrets"] = list(secrets)
        r = self._cloud._request("POST", "/v1/sandboxes", body)
        box = CloudSandbox(self._cloud, r["sandbox"])
        if wait and box.state not in _SETTLED:
            box.wait_until_running(timeout=wait_timeout)
        if labels is not None:
            box.set_labels(labels)
        return box

    def list(  # noqa: A003
        self,
        *,
        all: bool = False,  # noqa: A002 (the API's name)
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        name: Optional[str] = None,
        labels: Optional[Mapping[str, Union[str, bool]]] = None,
    ) -> List["CloudSandbox"]:
        """The org's sandboxes, newest first; with ``all``, deleted ones too.

        With ``limit`` or ``cursor`` this is one page: :meth:`list_page` also
        gives the next cursor, and :meth:`iter` walks every page.
        """
        return self.list_page(all=all, limit=limit, cursor=cursor, name=name, labels=labels).sandboxes

    def list_page(
        self,
        *,
        all: bool = False,  # noqa: A002
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        name: Optional[str] = None,
        labels: Optional[Mapping[str, Union[str, bool]]] = None,
    ) -> CloudSandboxPage:
        """One page of sandboxes and the cursor of the next (``None`` at the end).

        ``name`` finds a sandbox by its name; ``labels`` (``{"team": "ml",
        "gpu": True}``) keeps only those with every label (``True``: any value).
        """
        q: List[tuple] = []
        if all:
            q.append(("all", "1"))
        if limit is not None:
            if not _is_int(limit) or limit < 1:
                raise ValueError("limit must be a positive whole number")
            q.append(("limit", str(limit)))
        if cursor:
            q.append(("cursor", cursor))
        if name is not None:
            q.append(("name", name))
        for k, v in (labels or {}).items():
            q.append(("label", k if v is True else f"{k}={v}"))
        path = "/v1/sandboxes" + ("?" + urllib.parse.urlencode(q) if q else "")
        r = self._cloud._request("GET", path) or {}
        nxt = r.get("next_cursor")
        return CloudSandboxPage(
            sandboxes=[CloudSandbox(self._cloud, s) for s in r.get("sandboxes", [])],
            next_cursor=nxt if isinstance(nxt, str) and nxt else None,
        )

    def iter(self, *, all: bool = False, page_size: Optional[int] = None) -> Iterator["CloudSandbox"]:  # noqa: A002,A003
        """Every sandbox, following ``next_cursor`` page by page.

        A server that does not page answers everything at once, which is then
        the only page.
        """
        cursor: Optional[str] = None
        seen = set()
        while True:
            page = self.list_page(all=all, limit=page_size, cursor=cursor)
            yield from page.sandboxes
            if not page.next_cursor or page.next_cursor in seen:
                return
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def get(self, sandbox_id: str) -> "CloudSandbox":
        r = self._cloud._request("GET", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id, safe='')}")
        return CloudSandbox(self._cloud, r["sandbox"])


class CloudSandbox:
    """One cloud sandbox. ``info`` is its state as of the last call.

    ``checkpoint``, ``checkpoints``, ``rewind``, ``fork``, ``ports`` and
    ``files`` need the host's data plane. On a host without it the API
    answers 501 (``not_available`` or ``not_supported``), raised as
    :class:`~kosa8.Kosa8UnsupportedError`; not every kosa8 Cloud host has it yet.
    """

    def __init__(self, cloud: Kosa8Cloud, info: Dict[str, Any]) -> None:
        self._cloud = cloud
        self.info = info

    def __repr__(self) -> str:
        return f"CloudSandbox(id={self.id!r}, state={self.state!r})"

    @property
    def id(self) -> str:
        return self.info["id"]

    @property
    def state(self) -> str:
        return self.info.get("state", "")

    @property
    def files(self) -> "CloudFiles":
        """Files in the sandbox (data plane; may answer 501 ``not_supported`` on current hosts)."""
        return CloudFiles(self)

    def _path(self, suffix: str = "") -> str:
        return f"/v1/sandboxes/{urllib.parse.quote(self.id, safe='')}{suffix}"

    def exec(
        self,
        cmd: Union[str, Sequence[str]],
        *,
        image: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CloudExecResult:
        """Run a command and wait for it. A string runs through ``sh -c``.
        A sleeping sandbox wakes for it.

        Each exec runs in a fresh container: only ``/workspace`` persists, so
        ``cd``, ``export`` and packages installed elsewhere are gone by the
        next one. kosa8 Cloud cuts a buffered exec off after about 30 s, and
        this raises :class:`~kosa8.Kosa8ExecTimeoutError`; run long work in
        the background (``nohup sh -c '...' > /workspace/job.log 2>&1 &``) and
        poll it. The request (command and env) is at most 64 KiB.
        """
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        body: Dict[str, Any] = {"cmd": argv}
        if image is not None:
            body["image"] = image
        if env:
            body["env"] = [f"{k}={v}" for k, v in env.items()]
        r = self._cloud._request("POST", self._path("/exec"), body)
        return CloudExecResult(
            exit_code=r.get("exit"),
            stdout=r.get("stdout") or "",
            stderr=r.get("stderr") or "",
            woke_ms=r.get("woke_ms"),
        )

    def sleep(self) -> "CloudSandbox":
        """Snapshot it and free its VM; it then costs only snapshot storage."""
        self.info = self._cloud._request("POST", self._path("/sleep"))["sandbox"]
        return self

    def wake(self) -> "CloudSandbox":
        """Restore a sleeping sandbox, files and all."""
        self.info = self._cloud._request("POST", self._path("/wake"))["sandbox"]
        return self

    def refresh(self) -> "CloudSandbox":
        self.info = self._cloud._request("GET", self._path())["sandbox"]
        return self

    def delete(self) -> None:
        self._cloud._request("DELETE", self._path())

    def update(
        self,
        *,
        ttl_s: Optional[int] = None,
        delete_after: Optional[int] = None,
        sleep_after_s: Optional[int] = None,
        clear_lifetime: bool = False,
    ) -> "CloudSandbox":
        """Change its lifetime or sleep timer.

        ``ttl_s`` counts from now; ``clear_lifetime=True`` removes the
        automatic delete. A new ``sleep_after_s`` is recorded; the host keeps
        the idle time the sandbox started with until it can change it in place.
        """
        if clear_lifetime and (ttl_s is not None or delete_after is not None):
            raise ValueError("clear_lifetime cannot be combined with ttl_s or delete_after")
        _check_lifetime(ttl_s, delete_after)
        _check_sleep_after(sleep_after_s)
        body: Dict[str, Any] = {}
        if clear_lifetime:
            body["ttl_s"] = None
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        if delete_after is not None:
            body["delete_after"] = delete_after
        if sleep_after_s is not None:
            body["sleep_after_s"] = sleep_after_s
        if not body:
            raise ValueError("give ttl_s, delete_after, sleep_after_s or clear_lifetime")
        self.info = self._cloud._request("PATCH", self._path(), body)["sandbox"]
        return self

    def checkpoint(self, label: Optional[str] = None) -> Dict[str, Any]:
        """Save memory, processes and disk as a checkpoint and return it
        (``id``, ``label``, ``created_at``, ``bytes``). The newest 10 are kept.

        Data plane: may answer 501 ``not_supported`` on current hosts.
        """
        body: Dict[str, Any] = {}
        if label is not None:
            if len(label) > 200:
                raise ValueError("a checkpoint label is at most 200 characters")
            body["label"] = label
        return self._cloud._request("POST", self._path("/checkpoints"), body)["checkpoint"]

    def checkpoints(self) -> List[Dict[str, Any]]:
        """Its checkpoints, oldest first. Does not wake a sleeping sandbox.

        Data plane: may answer 501 ``not_supported`` on current hosts.
        """
        return (self._cloud._request("GET", self._path("/checkpoints")) or {}).get("checkpoints", [])

    def rewind(self, checkpoint_id: str) -> CloudRewindResult:
        """Go back to a checkpoint. The sandbox as it was is first saved as an
        automatic checkpoint, returned as ``backup``.

        Data plane: may answer 501 ``not_supported`` on current hosts.
        """
        if not checkpoint_id:
            raise ValueError("rewind needs a checkpoint id")
        r = self._cloud._request("POST", self._path("/rewind"), {"checkpoint_id": checkpoint_id}) or {}
        if isinstance(r.get("sandbox"), dict):
            self.info = r["sandbox"]
        return CloudRewindResult(checkpoint=r.get("checkpoint") or {}, backup=r.get("backup"), ms=r.get("ms"))

    def fork(self, n: int = 1) -> List["CloudSandbox"]:
        """``n`` (1 to 100) live copies of the sandbox as it is now, on its
        host. Each is held to the rules of a create (payment, quotas); all or
        nothing.

        Data plane: may answer 501 ``not_supported`` on current hosts.
        """
        if not _is_int(n) or not 1 <= n <= MAX_FORK:
            raise ValueError(f"n must be a whole number from 1 to {MAX_FORK}")
        r = self._cloud._request("POST", self._path("/fork"), {"n": n}) or {}
        return [CloudSandbox(self._cloud, s) for s in r.get("sandboxes", [])]

    def ports(self) -> List[Dict[str, Any]]:
        """TCP ports listening in its running containers (``port``, ``address``, ``container``, ``loopback``).

        Data plane: may answer 501 ``not_supported`` on current hosts.
        """
        return (self._cloud._request("GET", self._path("/ports")) or {}).get("ports", [])

    def wait_until_running(self, *, timeout: float = 300.0, interval: float = 1.0) -> "CloudSandbox":
        """Poll while it boots (``creating``); raises if it ends in ``error``
        (with its ``last_error``) or the time runs out."""
        deadline = time.monotonic() + timeout
        while self.state not in _SETTLED:
            if time.monotonic() + interval > deadline:
                raise Kosa8TimeoutError(f"sandbox {self.id} still {self.state} after {timeout} s")
            time.sleep(interval)
            self.refresh()
        if self.state == "error":
            raise Kosa8Error(f"sandbox {self.id} failed to start: {self.info.get('last_error') or 'no reason given'}")
        return self

    def exec_background(
        self,
        cmd: Union[str, Sequence[str]],
        *,
        image: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CloudBackgroundExec:
        """Start a command that keeps running after this returns; read it
        with ``.status()`` or wait for it with ``.wait()``::

            job = box.exec_background("npm run build")
            done = job.wait()
            print(done["exit"], done["output"])
        """
        body: Dict[str, Any] = {"cmd": argv_of(cmd), "background": True}
        if image is not None:
            body["image"] = image
        el = env_list(env)
        if el:
            body["env"] = el
        r = self._cloud._request("POST", self._path("/exec"), body)
        return CloudBackgroundExec(self._cloud, self._path(), r["exec"])

    def get_exec(self, exec_id: str) -> CloudBackgroundExec:
        """A background command by id (``bg_...``)."""
        job = CloudBackgroundExec(self._cloud, self._path(), {"id": exec_id, "state": "running"})
        job.status()
        return job

    def snapshot(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Save it as a durable snapshot (memory, processes and disk), kept
        until deleted whatever happens to the sandbox. Start copies with
        ``cloud.sandboxes.create(snapshot=snap["id"])``."""
        body = {"name": name} if name is not None else {}
        return self._cloud._request("POST", self._path("/snapshots"), body)["snapshot"]

    @property
    def previews(self) -> CloudPreviews:
        """Its ports at https URLs: ``box.previews.create(3000)["url"]``."""
        return CloudPreviews(self._cloud, self._path())

    @property
    def schedules(self) -> CloudSchedules:
        """Commands it runs on a cron schedule."""
        return CloudSchedules(self._cloud, self._path())

    @property
    def code_contexts(self) -> CloudCodeContexts:
        """Its code-interpreter contexts."""
        return CloudCodeContexts(self._cloud, self._path())

    def run_code(
        self, code: str, *, context: Optional[str] = None, language: Optional[str] = None, timeout_s: Optional[float] = None
    ) -> Dict[str, Any]:
        """Run a Python cell in a stateful context (globals persist between
        calls). Answers ``results`` (text, ``image/png`` charts, ...),
        ``stdout``, ``stderr`` and ``error`` when it raised."""
        body: Dict[str, Any] = {"code": code}
        if context is not None:
            body["context"] = context
        if language is not None:
            body["language"] = language
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        return self._cloud._request("POST", self._path("/code"), body)

    def terminal(
        self, *, cols: Optional[int] = None, rows: Optional[int] = None, command: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """A ``wss://`` URL for an interactive terminal (PTY): ``url``, ``expires_at``, ``protocol``."""
        body: Dict[str, Any] = {}
        if cols is not None:
            body["cols"] = cols
        if rows is not None:
            body["rows"] = rows
        if command is not None:
            body["command"] = list(command)
        return self._cloud._request("POST", self._path("/terminal"), body)["terminal"]

    def egress(self) -> Dict[str, Any]:
        """The egress policy it runs with."""
        return self._cloud._request("GET", self._path("/egress"))["egress"]

    def set_egress(
        self,
        *,
        internet: Optional[bool] = None,
        allow: Optional[Sequence[str]] = None,
        deny: Optional[Sequence[str]] = None,
        presets: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Change what it may reach, at once (a sleeping sandbox when it wakes)."""
        body: Dict[str, Any] = {}
        if internet is not None:
            body["internet"] = internet
        for k, v in (("allow", allow), ("deny", deny), ("presets", presets)):
            if v is not None:
                body[k] = list(v)
        return self._cloud._request("PUT", self._path("/egress"), body)["egress"]

    def reset_egress(self) -> Dict[str, Any]:
        """Back to the organization's default egress policy."""
        return self._cloud._request("DELETE", self._path("/egress"))["egress"]

    def secrets(self) -> List[str]:
        """Names of the org secrets it holds (never values)."""
        return (self._cloud._request("GET", self._path("/secrets")) or {}).get("secrets", [])

    def set_labels(self, labels: Mapping[str, str]) -> Dict[str, str]:
        """Replace its labels (up to 16); ``{}`` clears them."""
        r = self._cloud._request("PUT", self._path("/labels"), {"labels": dict(labels)}) or {}
        self.info = {**self.info, "labels": r.get("labels", {})}
        return r.get("labels", {})

    def metrics(self) -> Dict[str, Any]:
        """CPU, memory, disk and network use, read on the host."""
        return self._cloud._request("GET", self._path("/metrics"))["metrics"]

    def logs(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Its last commands, with exit codes and output tails."""
        q = f"?limit={int(limit)}" if limit is not None else ""
        return (self._cloud._request("GET", self._path(f"/logs{q}")) or {}).get("entries", [])


class CloudFiles:
    """Files in a cloud sandbox, at most 8 MiB each. Paths are absolute, such
    as ``/workspace/app.py``; only ``/workspace`` outlives an exec. A sleeping
    sandbox wakes first.

    Data plane: on a host without it the API answers 501 (``not_available``
    or ``not_supported``), raised as :class:`~kosa8.Kosa8UnsupportedError`.
    """

    def __init__(self, sandbox: CloudSandbox) -> None:
        self._sb = sandbox

    def _q(self, suffix: str, path: str, **extra: str) -> str:
        if not path:
            raise ValueError("a path is required, e.g. /workspace/app.py")
        return self._sb._path(suffix) + "?" + urllib.parse.urlencode({"path": path, **extra})

    def read(self, path: str) -> bytes:
        """The file's bytes."""
        return self._sb._cloud._request("GET", self._q("/files", path), raw=True)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def write(self, path: str, content: Union[bytes, str], encoding: str = "utf-8") -> Dict[str, Any]:
        """Create or replace a file; returns its info (``path``, ``size``, ...)."""
        data = content.encode(encoding) if isinstance(content, str) else bytes(content)
        return (self._sb._cloud._request("PUT", self._q("/files", path), data=data) or {}).get("file", {})

    def list(self, path: str = "/workspace") -> List[Dict[str, Any]]:  # noqa: A003
        """A folder's entries: ``name``, ``path``, ``type`` (file or dir), ``size``, ``mode``, ``mod_time``."""
        return (self._sb._cloud._request("GET", self._q("/files/list", path)) or {}).get("files", [])

    def mkdir(self, path: str) -> Dict[str, Any]:
        """Make a folder and its parents."""
        if not path:
            raise ValueError("a path is required, e.g. /workspace/data")
        return (self._sb._cloud._request("POST", self._sb._path("/files/mkdir"), {"path": path}) or {}).get("file", {})

    def read_raw(self, path: str) -> bytes:
        """The file's bytes with no 8 MiB cap (the streamed endpoint)."""
        return self._sb._cloud._request("GET", self._q("/files/raw", path), raw=True)

    def write_raw(self, path: str, content: Union[bytes, str], encoding: str = "utf-8") -> Dict[str, Any]:
        """Create or replace a file with no 8 MiB cap (the streamed endpoint)."""
        data = content.encode(encoding) if isinstance(content, str) else bytes(content)
        r = self._sb._cloud._request("PUT", self._q("/files/raw", path), data=data)
        return (r or {}).get("file", {}) if isinstance(r, dict) else {}

    def link(self, path: str, *, method: Optional[str] = None, ttl_s: Optional[int] = None) -> Dict[str, Any]:
        """A presigned URL for one download (``GET``, the default) or upload
        (``PUT``) of ``path``, straight to the host: hand it to curl for big
        files. ``ttl_s`` is 10 to 300."""
        if not path:
            raise ValueError("a path is required, e.g. /workspace/app.py")
        body: Dict[str, Any] = {"path": path}
        if method is not None:
            if method not in ("GET", "PUT"):
                raise ValueError("method is GET or PUT")
            body["method"] = method
        if ttl_s is not None:
            if not _is_int(ttl_s) or not 10 <= ttl_s <= 300:
                raise ValueError("ttl_s must be a whole number from 10 to 300")
            body["ttl_s"] = ttl_s
        return self._sb._cloud._request("POST", self._sb._path("/files/link"), body)["link"]

    def delete(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file, or a folder and everything in it with ``recursive``."""
        extra = {"recursive": "1"} if recursive else {}
        self._sb._cloud._request("DELETE", self._q("/files", path, **extra))


class CloudFunctions:
    def __init__(self, cloud: Kosa8Cloud) -> None:
        self._cloud = cloud

    def _path(self, name: str, suffix: str = "") -> str:
        return f"/v1/functions/{urllib.parse.quote(name, safe='')}{suffix}"

    def deploy(self, name: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
        """Deploy (create or replace) a function; every deploy bumps its version."""
        return self._cloud._request("PUT", self._path(name), dict(spec))

    def list(self) -> List[Dict[str, Any]]:
        return self._cloud._request("GET", "/v1/functions")["functions"]

    def get(self, name: str) -> Dict[str, Any]:
        return self._cloud._request("GET", self._path(name))

    def delete(self, name: str) -> None:
        self._cloud._request("DELETE", self._path(name))

    def invoke(self, name: str, event: Any = None) -> Dict[str, Any]:
        """Call it once. A handler that raises answers ``ok: False``, not an exception."""
        return self._cloud._request("POST", self._path(name, "/invoke"), {"event": event})

    def map(self, name: str, events: Sequence[Any], *, concurrency: Optional[int] = None) -> Dict[str, Any]:
        """One call per event, results in input order."""
        body: Dict[str, Any] = {"events": list(events)}
        if concurrency is not None:
            body["concurrency"] = concurrency
        return self._cloud._request("POST", self._path(name, "/map"), body)
