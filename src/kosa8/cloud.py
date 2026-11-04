"""kosa8 Cloud: the same sandboxes, run on kosa8's hosts (or your own, with
bring-your-own-cloud) instead of the local daemon.

>>> from kosa8 import Kosa8Cloud
>>> cloud = Kosa8Cloud()  # KOSA8_API_KEY from the environment
>>> box = cloud.sandboxes.create(template="python")
>>> box.exec(["python3", "-c", "print(6 * 7)"]).stdout
'42\\n'
>>> box.delete()

Authentication is an API key (``k8c_live_...``) made in the cloud dashboard.
Errors are the SDK's usual typed errors; each carries the cloud's stable
``code`` (for example ``payment_required``, ``quota_exceeded``).

Standard library only, like the rest of the SDK.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from .errors import Kosa8ConnectionError, Kosa8Error, Kosa8TimeoutError, error_from_response

DEFAULT_CLOUD_URL = "https://api.kosa8.com"
CLOUD_KEY_PREFIX = "k8c_live_"

#: (method, url, headers, body bytes or None, timeout) -> (status, body bytes)
Opener = Callable[[str, str, Dict[str, str], Optional[bytes], float], "tuple[int, bytes]"]


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
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class CloudExecResult:
    exit_code: Optional[int]
    stdout: str
    stderr: str
    #: Set when the sandbox was asleep and woke for this command.
    woke_ms: Optional[int] = None


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

    def __repr__(self) -> str:  # never the key
        return f"Kosa8Cloud(base_url={self.base_url!r})"

    def me(self) -> Dict[str, Any]:
        """The org, the key in use and, for a member's key, the member."""
        return self._request("GET", "/v1/me")

    def usage(self) -> Dict[str, Any]:
        """This month's usage, bill estimate and whether work is paused for payment."""
        return self._request("GET", "/v1/usage")

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            status, raw = self._open(method, self.base_url + path, headers, data, self.timeout)
        except (socket.timeout, TimeoutError) as e:
            raise Kosa8TimeoutError(f"kosa8 Cloud did not answer within {self.timeout} s", method=method, path=path) from e
        except (urllib.error.URLError, OSError) as e:
            raise Kosa8ConnectionError(f"could not reach kosa8 Cloud at {self.base_url}: {e}", method=method, path=path) from e
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            parsed: Any = json.loads(text) if text else None
        except ValueError:
            parsed = text
        if status >= 400:
            raise _cloud_error(status, parsed, method, path)
        return parsed


def _cloud_error(status: int, body: Any, method: str, path: str) -> Kosa8Error:
    # The cloud answers {error: code, message}: the sentence is the message,
    # and the code is kept.
    code = None
    msg_body = body
    if isinstance(body, dict):
        if isinstance(body.get("error"), str):
            code = body["error"]
        msg_body = {"error": body.get("message") or body.get("error")}
    e = error_from_response(status, msg_body, method=method, path=path, code=code)
    e.body = body
    return e


class CloudSandboxes:
    def __init__(self, cloud: Kosa8Cloud) -> None:
        self._cloud = cloud

    def create(
        self,
        *,
        template: Optional[str] = None,
        region: Optional[str] = None,
        sleep_after_s: Optional[int] = None,
    ) -> "CloudSandbox":
        """Start a sandbox; returns once it is running."""
        body: Dict[str, Any] = {}
        if template is not None:
            body["template"] = template
        if region is not None:
            body["region"] = region
        if sleep_after_s is not None:
            body["sleep_after_s"] = sleep_after_s
        r = self._cloud._request("POST", "/v1/sandboxes", body)
        return CloudSandbox(self._cloud, r["sandbox"])

    def list(self, *, all: bool = False) -> List["CloudSandbox"]:  # noqa: A002 (the API's name)
        """The org's sandboxes; with all, deleted ones too."""
        r = self._cloud._request("GET", "/v1/sandboxes" + ("?all=1" if all else ""))
        return [CloudSandbox(self._cloud, s) for s in r["sandboxes"]]

    def get(self, sandbox_id: str) -> "CloudSandbox":
        r = self._cloud._request("GET", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id, safe='')}")
        return CloudSandbox(self._cloud, r["sandbox"])


class CloudSandbox:
    """One cloud sandbox. ``info`` is its state as of the last call."""

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
        A sleeping sandbox wakes for it."""
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
