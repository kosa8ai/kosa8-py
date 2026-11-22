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
(busybox ``sh``; no bash, python or git). A buffered exec runs at most
90 seconds (:class:`~kosa8.Kosa8ExecTimeoutError`), and an exec request
(command and env) is at most 64 KiB.

The calls that make sandboxes (create, create_many, fork, grade) send an
``Idempotency-Key`` that their retries reuse, so a retry after a timeout or a
5xx gets the first answer instead of a second fleet.

Standard library only, like the rest of the SDK.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from .errors import (
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
    classify_failure,
    error_from_response,
)
from .retry import RetryPolicy, call_with_retry
from .jobs import CloudJobs
from .cloud_features import (
    CloudBackgroundExec,
    CloudCodeContexts,
    CloudPipelines,
    CloudTemplates,
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
USER_AGENT = "kosa8-python/0.5.2"

#: A ttl_s is from 60 seconds to 90 days.
MIN_TTL_S = 60
MAX_TTL_S = 7776000
#: sleep_after_s is from 0 (never) to 7 days.
MAX_SLEEP_AFTER_S = 604800
#: A fork call makes from 1 to 100 copies (``fork`` makes more in several calls).
MAX_FORK = 100
#: A batch create makes from 1 to 1000 sandboxes.
MAX_BATCH = 1000
#: Copies one fork call makes at most (kosa8 Cloud makes them 100 at a time on the parent's host).
MAX_FORK_PER_CALL = 1000
#: A lease is 60 s to 24 hours.
MIN_LEASE_S = 60
MAX_LEASE_S = 86400
#: A grade's hidden files: at most 64, adding up to 8 MiB before base64.
GRADE_MAX_FILES = 64
GRADE_MAX_BYTES = 8 * 1024 * 1024
#: A recorded rollout's meta is at most 8 KiB as JSON, its stdout tail 4 KiB.
META_MAX = 8192
ROLLOUT_STDOUT_TAIL = 4096
#: The label kosa8 Cloud groups a run's sandboxes by.
RUN_LABEL = "kosa8.run"
#: The limit kosa8 Cloud puts on a buffered exec, in seconds (504 exec_timeout past it).
EXEC_LIMIT_S = 90
#: vCPU x GiB sizes a sized create takes.
CLOUD_SIZES = ("2x4", "4x8", "8x16", "16x32")
#: States a booting sandbox does not leave on its own.
_SETTLED = frozenset({"running", "error", "deleted", "stopped", "host_removed", "host_lost", "sleeping", "paused"})

#: (method, url, headers, body bytes or None, timeout) -> (status, body bytes),
#: or (status, body bytes, response headers).
Opener = Callable[[str, str, Dict[str, str], Optional[bytes], float], Any]

_EXEC_HINT = (
    f"a buffered exec is stopped after {EXEC_LIMIT_S} s; run long work in the background, "
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
    #: ``ok`` (exit 0) or ``exit_nonzero``, as the API sent it (worked out from
    #: the exit code when it sent none).
    failure_class: Optional[str] = None


@dataclass
class GradeResult:
    """What :meth:`CloudSandbox.grade` answered.

    ``reward`` is the grader's: the last stdout line that is JSON with a
    numeric ``reward`` (and optional ``metrics``; ``reward_from="json"``),
    else 1.0 for exit 0 and 0.0 otherwise (``reward_from="exit"``). When the
    command did not finish (it timed out, or the host failed) ``reward`` is
    ``None`` and ``failure_class`` is ``timeout`` or ``infra``: mask it, do
    not score it 0. ``retryable`` says whether grading again may work.
    """

    reward: Optional[float]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    #: ``ok``, ``exit_nonzero``, ``timeout`` or ``infra``.
    failure_class: str
    metrics: Dict[str, Any]
    ms: Optional[int] = None
    #: The id of the sealed fork the grader ran in (deleted unless ``kept``).
    grader_id: Optional[str] = None
    #: ``json``, ``exit`` or ``None`` (no reward).
    reward_from: Optional[str] = None
    retryable: bool = False
    #: Whether the grader fork was kept (``keep=True``).
    kept: bool = False
    #: The graded sandbox.
    sandbox_id: Optional[str] = None
    #: The run it was recorded on (``run_id=``), and whether that worked.
    #: ``recorded`` is ``None`` without a run; ``False`` if recording failed
    #: (the grade itself still stands).
    run_id: Optional[str] = None
    recorded: Optional[bool] = None
    #: The error code and message when the command did not finish.
    error: Optional[str] = None
    message: Optional[str] = None
    #: The whole answer, for fields this SDK does not know yet.
    raw: Optional[Dict[str, Any]] = None


class SandboxBatch(list):
    """The sandboxes a batch create or a fork made: a plain ``list`` of
    :class:`CloudSandbox`, plus ``failed`` (one dict per sandbox that was not
    made: ``index``, ``code``, ``message``, ``failure_class``, ``retryable``),
    ``requested`` and ``run_id``.

    A call that made none raises the first refusal instead (its ``.body``
    has ``requested``, ``created: 0`` and ``failed``), so an empty batch is
    never returned."""

    def __init__(self, sandboxes: Sequence[Any] = (), *, failed: Optional[List[Dict[str, Any]]] = None,
                 requested: Optional[int] = None, run_id: Optional[str] = None) -> None:
        super().__init__(sandboxes)
        self.failed: List[Dict[str, Any]] = list(failed or [])
        self.requested: int = len(self) + len(self.failed) if requested is None else requested
        self.run_id = run_id

    @property
    def created(self) -> int:
        return len(self)


def _exec_result(r: Mapping[str, Any]) -> CloudExecResult:
    code = r.get("exit")
    fc = r.get("failure_class")
    if not isinstance(fc, str):
        fc = None if code is None else ("ok" if code == 0 else "exit_nonzero")
    return CloudExecResult(
        exit_code=code, stdout=r.get("stdout") or "", stderr=r.get("stderr") or "",
        woke_ms=r.get("woke_ms"), failure_class=fc,
    )


def _grade_result(r: Mapping[str, Any]) -> GradeResult:
    code = r.get("exit")
    if not _is_int(code):
        code = None
    reward: Any = r.get("reward")
    rfrom = r.get("reward_from")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        reward = None
        if "reward" not in r and code is not None:  # an answer without the field: by exit code
            reward, rfrom = (1.0 if code == 0 else 0.0), "exit"
    fc = r.get("failure_class")
    if not isinstance(fc, str):
        fc = "infra" if code is None else ("ok" if code == 0 else "exit_nonzero")
    rt = r.get("retryable")
    metrics = r.get("metrics")
    rec = r.get("recorded")
    return GradeResult(
        reward=None if reward is None else float(reward), exit_code=code, stdout=r.get("stdout") or "",
        stderr=r.get("stderr") or "", failure_class=fc, metrics=dict(metrics) if isinstance(metrics, dict) else {},
        ms=r.get("ms"), grader_id=r.get("grader_id"), reward_from=rfrom if isinstance(rfrom, str) else None,
        retryable=rt if isinstance(rt, bool) else fc == "infra", kept=r.get("kept") is True,
        sandbox_id=r.get("sandbox_id"), run_id=r.get("run_id"), recorded=rec if isinstance(rec, bool) else None,
        error=r.get("error"), message=r.get("message"), raw=dict(r),
    )


def _check_lease(lease_s: Optional[int]) -> None:
    if lease_s is not None and (not _is_int(lease_s) or not MIN_LEASE_S <= lease_s <= MAX_LEASE_S):
        raise ValueError(f"lease_s must be a whole number of seconds from {MIN_LEASE_S} to {MAX_LEASE_S}")


def _check_labels(labels: Optional[Mapping[str, str]]) -> None:
    if labels is not None and len(labels) > 16:
        raise ValueError("a sandbox has at most 16 labels")


def _selector(run_id: Optional[str], labels: Optional[Mapping[str, str]], ids: Optional[Sequence[str]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if run_id:
        body["run_id"] = run_id
    if labels:
        body["labels"] = dict(labels)
    if ids:
        body["ids"] = [i.id if hasattr(i, "id") else str(i) for i in ids]
    if not body:
        raise ValueError("give run_id, labels or ids: which sandboxes")
    return body


def _ambient(cloud: Any) -> Dict[str, Any]:
    """The run :class:`kosa8.rl.Run` set on this client (run_id, lease_s), if any."""
    return dict(getattr(cloud, "_run_defaults", None) or {})


def _fork_body(n: int, labels: Optional[Mapping[str, str]], run_id: Optional[str], lease_s: Optional[int],
               partial_ok: Optional[bool], prefix: Optional[str] = None) -> Dict[str, Any]:
    _check_lease(lease_s)
    _check_labels(labels)
    body: Dict[str, Any] = {"n": n}
    if prefix is not None:
        if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
            raise ValueError("prefix must be 1-59 letters, digits, '.', '_' or '-', starting with a letter or digit")
        body["prefix"] = prefix
    if labels:
        body["labels"] = dict(labels)
    if run_id:
        body["run_id"] = run_id
    if lease_s is not None:
        body["lease_s"] = lease_s
    if partial_ok is not None:
        body["partial_ok"] = partial_ok
    return body


def _batch_body(
    n: int, *, template: Optional[str], image: Optional[str], cpus: Optional[int], mem_mb: Optional[int],
    region: Optional[str], labels: Optional[Mapping[str, str]], ttl_s: Optional[int], lease_s: Optional[int],
    run_id: Optional[str], partial_ok: bool, prefix: Optional[str] = None,
) -> Dict[str, Any]:
    if not _is_int(n) or not 1 <= n <= MAX_BATCH:
        raise ValueError(f"n must be a whole number from 1 to {MAX_BATCH}")
    if (cpus is None) != (mem_mb is None):
        raise ValueError("give cpus and mem_mb together")
    _check_lifetime(ttl_s, None)
    _check_lease(lease_s)
    _check_labels(labels)
    if lease_s is not None and ttl_s is not None:
        raise ValueError("give lease_s or ttl_s, not both: a lease is a TTL that heartbeats move forward")
    if prefix is not None and (not isinstance(prefix, str) or not _PREFIX_RE.match(prefix)):
        raise ValueError("prefix must be 1-59 letters, digits, '.', '_' or '-', starting with a letter or digit")
    body: Dict[str, Any] = {"count": n, "partial_ok": partial_ok}
    if prefix is not None:
        body["prefix"] = prefix
    for k, v in (("template", template), ("image", image), ("cpus", cpus), ("mem_mb", mem_mb),
                 ("region", region), ("ttl_s", ttl_s), ("lease_s", lease_s), ("run_id", run_id)):
        if v is not None:
            body[k] = v
    if labels:
        body["labels"] = dict(labels)
    return body


_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,58}$")


def _grade_body(
    cmd: Union[str, Sequence[str]], *, files: Optional[Mapping[str, Union[bytes, str]]], timeout_s: Optional[float],
    env: Optional[Mapping[str, str]], keep: bool, run_id: Optional[str], rollout: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"cmd": argv_of(cmd)}
    if files:
        if len(files) > GRADE_MAX_FILES:
            raise ValueError(f"a grade takes at most {GRADE_MAX_FILES} files, got {len(files)}")
        enc: Dict[str, str] = {}
        total = 0
        for path, content in files.items():
            if (not isinstance(path, str) or not path.startswith("/") or "\0" in path or len(path) > 4096
                    or ".." in path.split("/")):
                raise ValueError(f"a grader file path must be absolute, without '..', got {path!r}")
            raw = content.encode() if isinstance(content, str) else bytes(content)
            total += len(raw)
            if total > GRADE_MAX_BYTES:
                raise ValueError(
                    f"a grade's files add up to at most {GRADE_MAX_BYTES // (1024 * 1024)} MiB "
                    f"(before base64); past that at {path!r}. Put large fixtures in the image or "
                    "the sandbox, and only the hidden tests in files="
                )
            enc[path] = base64.b64encode(raw).decode("ascii")
        body["files"] = enc
    if timeout_s is not None:
        if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s != int(timeout_s)
                or not 1 <= timeout_s <= EXEC_LIMIT_S):
            raise ValueError(f"timeout_s must be a whole number of seconds from 1 to {EXEC_LIMIT_S}")
        body["timeout_s"] = int(timeout_s)
    el = env_list(env)
    if el:
        body["env"] = el
    if keep:
        body["keep"] = True
    if run_id:
        body["run_id"] = run_id
    if rollout:
        ro = dict(rollout)
        if ro.get("group") is not None:
            ro["group"] = str(ro["group"])[:128]  # the grader takes a string
        if ro.get("meta") is not None:
            ro["meta"] = fit_meta(ro["meta"])
        body["rollout"] = ro
    return body


def fit_meta(meta: Mapping[str, Any], limit: int = META_MAX) -> Dict[str, Any]:
    """``meta`` cut to what a recorded rollout keeps (8 KiB as JSON): keys
    are dropped from the end, longest values first, and ``"truncated": true``
    is added. Values that are not JSON are turned into strings."""
    m = json.loads(json.dumps(dict(meta), default=str))

    def size(d: Mapping[str, Any]) -> int:
        return len(json.dumps(d, separators=(",", ":"), ensure_ascii=False))

    if size(m) <= limit:
        return m
    m["truncated"] = True
    for k in sorted((k for k in m if k != "truncated"), key=lambda k: -size({k: m[k]})):
        if size(m) <= limit:
            break
        v = m.pop(k)
        if isinstance(v, str) and size(m) + len(k) + 8 < limit:
            m[k] = v[: max(0, limit - size(m) - len(k) - 8)]
    return m


def _batch_from(cloud: Any, r: Mapping[str, Any], make: Callable[[Any, Dict[str, Any]], Any], n: int) -> SandboxBatch:
    r = r or {}
    return SandboxBatch(
        [make(cloud, s) for s in r.get("sandboxes", [])],
        failed=list(r.get("failed") or []),
        requested=r.get("requested", n),
        run_id=r.get("run_id"),
    )


#: Methods that are safe to send twice. Other POSTs are retried only on 429
#: (refused before anything was done), unless they carry an Idempotency-Key.
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})
_IDEMPOTENT_POSTS = ("/v1/sandboxes/heartbeat", "/v1/sandboxes/drain")
#: The POSTs kosa8 Cloud keeps an Idempotency-Key for (24 hours): a retry with
#: the same key and body gets the first answer (``Idempotent-Replayed: true``).
_KEYED_POSTS = re.compile(
    r"^/v1/sandboxes(/batch|/[A-Za-z0-9_]{1,64}/(fork|grade))?$|^/v1/rl/runs/[A-Za-z0-9_]{1,64}/rollouts$|^/v1/jobs$")


def keyed_route(method: str, path: str) -> bool:
    """Whether kosa8 Cloud honours an Idempotency-Key on this call."""
    return method == "POST" and bool(_KEYED_POSTS.match(path.split("?", 1)[0]))


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _check_key(key: Optional[str]) -> None:
    if key is not None and (not isinstance(key, str) or not _KEY_RE.match(key)):
        raise ValueError("idempotency_key must be 1-255 printable ASCII characters, e.g. a UUID")


_KEY_RE = re.compile(r"^[\x21-\x7e]{1,255}$")


class _ScopedRetry:
    """A :class:`RetryPolicy` limited to what is safe for one request."""

    def __init__(self, policy: RetryPolicy, method: str, path: str, *, keyed: bool = False) -> None:
        self.policy = policy
        bare = path.split("?", 1)[0]
        self.idempotent = keyed or method in _IDEMPOTENT or bare in _IDEMPOTENT_POSTS

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        if not self.policy.should_retry(exc, attempt):
            return False
        return self.idempotent or getattr(exc, "status", None) == 429

    def delay(self, exc: Optional[BaseException], attempt: int) -> float:
        return self.policy.delay(exc, attempt)


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
        retry: Optional[RetryPolicy] = None,
    ) -> None:
        """``retry`` (default :class:`~kosa8.retry.RetryPolicy` ``()``: 3 tries,
        backoff with jitter) is applied to every request where trying again is
        safe: reads, deletes and heartbeats on infra failures, any request on a
        429 (waiting at least its ``Retry-After``). ``RetryPolicy(attempts=1)``
        turns it off."""
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
        self.retry = retry if retry is not None else RetryPolicy()
        self.sandboxes = CloudSandboxes(self)
        self.functions = CloudFunctions(self)
        #: Durable snapshots.
        self.snapshots = CloudSnapshots(self)
        #: Org secrets (values go in, never come out).
        self.secrets = CloudSecrets(self)
        #: CI runs, logs, artifacts and triggers.
        self.pipelines = CloudPipelines(self)
        #: The org's own templates: Dockerfile builds and bulk image imports.
        self.org_templates = CloudTemplates(self)
        #: Hosted jobs: kosa8 runs every task's rollout and grade server-side.
        self.jobs = CloudJobs(self)

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

    def fork_map(
        self,
        base: "CloudSandbox",
        fn: Callable[["CloudSandbox", Any], Any],
        items: Sequence[Any],
        *,
        concurrency: int = 16,
        retries: int = 2,
        timeout_s: Optional[float] = None,
        labels: Optional[Mapping[str, str]] = None,
        run_id: Optional[str] = None,
        lease_s: Optional[int] = None,
    ) -> List["ForkResult"]:
        """Run ``fn(child, item)`` for every item, each in its own fork of
        ``base``, ``concurrency`` at a time; results in input order.

        Children are forked in batches (up to 100 a call), always deleted when
        ``fn`` returns or raises, and deleted on Ctrl-C too. A failure is not
        raised: its :class:`ForkResult` has ``error`` and ``failure_class``.
        ``infra`` failures and rate limits are retried ``retries`` times on a
        fresh fork, with backoff and jitter. ``timeout_s`` gives up on an item
        (``failure_class="timeout"``) and deletes its child.
        """
        from ._forkmap import fork_map

        return fork_map(self, base, fn, items, concurrency=concurrency, retries=retries, timeout_s=timeout_s,
                        labels=labels, run_id=run_id, lease_s=lease_s)

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        data: Optional[bytes] = None,
        raw: bool = False,
        retry: Optional[RetryPolicy] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """One call, retried as :attr:`retry` allows. ``body`` is sent as JSON
        and ``data`` as raw bytes; with ``raw`` a successful answer's bytes are
        returned as they came. A call that makes sandboxes (:func:`keyed_route`)
        sends ``idempotency_key`` (a new UUID when not given), the same on every
        retry, so it may be retried after a timeout or a 5xx too."""
        extra = _idem_headers(method, path, idempotency_key)
        policy = _ScopedRetry(self.retry if retry is None else retry, method, path, keyed=bool(extra))
        return call_with_retry(lambda: self._request_once(method, path, body, data=data, raw=raw, extra=extra), policy)

    def _headers_payload(self, body: Any, data: Optional[bytes], raw: bool,
                         extra: Optional[Mapping[str, str]] = None) -> "tuple[Dict[str, str], Optional[bytes]]":
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/octet-stream" if raw else "application/json",
            "User-Agent": USER_AGENT,
        }
        if extra:
            headers.update(extra)
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
            payload = data
        elif body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        return headers, payload

    def _request_once(self, method: str, path: str, body: Any = None, *, data: Optional[bytes] = None,
                      raw: bool = False, extra: Optional[Mapping[str, str]] = None) -> Any:
        headers, payload = self._headers_payload(body, data, raw, extra)
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
        return _parse_answer(answer, method, path, raw)


def _idem_headers(method: str, path: str, key: Optional[str]) -> Dict[str, str]:
    _check_key(key)
    if key is None and not keyed_route(method, path):
        return {}
    return {"Idempotency-Key": key or new_idempotency_key()}


def _parse_answer(answer: Any, method: str, path: str, raw: bool) -> Any:
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
    # Older Workers gave up on the host before the 90 s limit and answered
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
    if getattr(e, "retry_after", None) is None and isinstance(body, dict):
        ra_s = body.get("retry_after_s")  # e.g. 409 idempotency_in_progress
        if isinstance(ra_s, (int, float)) and not isinstance(ra_s, bool) and ra_s > 0:
            e.retry_after = float(ra_s)
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


def _create_body(
    cloud: Any, *, template: Optional[str], region: Optional[str], sleep_after_s: Optional[int], ttl_s: Optional[int],
    delete_after: Optional[int], accept_price: Optional[float], size: Optional[str], cpus: Optional[int],
    mem_mb: Optional[int], image: Optional[str], disk_gb: Optional[int], persistent: Optional[bool],
    docker: Optional[bool], cpu: Optional[str], name: Optional[str], env: Optional[Mapping[str, str]],
    snapshot: Optional[Union[str, Mapping[str, Any]]], egress: Optional[Mapping[str, Any]],
    secrets: Optional[Sequence[str]], labels: Optional[Mapping[str, str]], lease_s: Optional[int],
    run_id: Optional[str],
) -> Dict[str, Any]:
    """The body of ``POST /v1/sandboxes`` (checked). Labels, ``kosa8.run``
    included, go in the body: kosa8 Cloud stores them before it answers, so a
    run's sandbox is found by drain and heartbeat while it boots."""
    _check_lifetime(ttl_s, delete_after)
    _check_sleep_after(sleep_after_s)
    amb = _ambient(cloud)
    lease_s = amb.get("lease_s") if lease_s is None else lease_s
    run_id = amb.get("run_id") if run_id is None else run_id
    _check_lease(lease_s)
    if run_id:
        labels = {**(labels or {}), RUN_LABEL: run_id}
    _check_labels(labels)
    body: Dict[str, Any] = {}
    if lease_s is not None:
        body["lease_s"] = lease_s
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
    if labels:
        body["labels"] = dict(labels)
    return body


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
        lease_s: Optional[int] = None,
        run_id: Optional[str] = None,
        wait: bool = True,
        wait_timeout: float = 300.0,
        idempotency_key: Optional[str] = None,
    ) -> "CloudSandbox":
        """Start a sandbox; returns once it is running.

        ``lease_s`` (60 s up) deletes it that long after the last heartbeat
        (:meth:`heartbeat`, or :func:`kosa8.rl.lease` in the background), so a
        crashed trainer cannot leak it. ``run_id`` labels it ``kosa8.run=<id>``
        for :meth:`drain` and the RL runs dashboard. Inside ``with
        kosa8.rl.Run(cloud, ...)`` both default to the run's.

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
        org secrets it holds, ``labels`` are sent with the create (stored
        before it answers, so they are there while it boots).

        ``template`` is a key from :meth:`Kosa8Cloud.templates` (default the
        org's default, ``base``: Alpine, busybox sh, no bash or python) or a
        kosa8 Store item, ``creator/name`` or ``creator/name@1.2.3``.
        ``ttl_s`` (60 s to 90 days) or ``delete_after`` (Unix ms) deletes it
        automatically: set one so a crashed script cannot leave it running.
        ``sleep_after_s`` is the idle time before it sleeps (0 never; default
        300). ``accept_price`` is a paid Store item's current price in USD,
        which the person agreed to.

        ``idempotency_key`` (default a new UUID) is sent as the
        ``Idempotency-Key`` header and reused on every retry, so a create that
        timed out or met a 5xx is retried without making a second sandbox.
        Pass your own to make a create safe to repeat across processes (kosa8
        Cloud keeps the answer 24 hours; the same key with another body is
        ``422 idempotency_mismatch``).
        """
        body = _create_body(
            self._cloud, template=template, region=region, sleep_after_s=sleep_after_s, ttl_s=ttl_s,
            delete_after=delete_after, accept_price=accept_price, size=size, cpus=cpus, mem_mb=mem_mb, image=image,
            disk_gb=disk_gb, persistent=persistent, docker=docker, cpu=cpu, name=name, env=env, snapshot=snapshot,
            egress=egress, secrets=secrets, labels=labels, lease_s=lease_s, run_id=run_id,
        )
        r = self._cloud._request("POST", "/v1/sandboxes", body, idempotency_key=idempotency_key)
        box = CloudSandbox(self._cloud, r["sandbox"])
        try:
            if wait and box.state not in _SETTLED:
                box.wait_until_running(timeout=wait_timeout)
        except BaseException:
            # It was made but cannot be handed back: do not leave it running.
            _quiet_delete(box)
            raise
        return box

    def create_many(
        self,
        n: int,
        *,
        template: Optional[str] = None,
        image: Optional[str] = None,
        cpus: Optional[int] = None,
        mem_mb: Optional[int] = None,
        region: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ttl_s: Optional[int] = None,
        lease_s: Optional[int] = None,
        run_id: Optional[str] = None,
        partial_ok: bool = True,
        prefix: Optional[str] = None,
        wait: bool = True,
        wait_timeout: float = 300.0,
        idempotency_key: Optional[str] = None,
    ) -> SandboxBatch:
        """Start ``n`` (1 to 1000) sandboxes in one call (``POST /v1/sandboxes/batch``).

        kosa8 Cloud paces the creates itself, so there is no 429 storm to
        retry. With ``partial_ok`` (the default) the ones it could not make
        (for example past the plan's running limit, ``code="running_limit"``)
        are in ``.failed``; with ``partial_ok=False`` it is all or nothing and
        the first failure is raised. Either way, if none could be made the
        first refusal is raised (its ``.body`` has ``requested``, ``created``
        and ``failed``). ``wait`` polls until each is running; a sandbox that
        fails to boot is deleted and moved to ``.failed``. The call carries an
        ``Idempotency-Key`` (``idempotency_key``, default a new UUID) that its
        retries reuse. ``prefix`` names them ``<prefix>-0001``, ``<prefix>-0002``, ...
        """
        amb = _ambient(self._cloud)
        body = _batch_body(
            n, template=template, image=image, cpus=cpus, mem_mb=mem_mb, region=region, labels=labels,
            ttl_s=ttl_s, lease_s=amb.get("lease_s") if lease_s is None else lease_s,
            run_id=amb.get("run_id") if run_id is None else run_id, partial_ok=partial_ok, prefix=prefix,
        )
        r = self._cloud._request("POST", "/v1/sandboxes/batch", body, idempotency_key=idempotency_key) or {}
        batch = _batch_from(self._cloud, r, CloudSandbox, n)
        if wait:
            _wait_all(batch, wait_timeout, partial_ok)
        return batch

    def heartbeat(
        self,
        *,
        run_id: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ids: Optional[Sequence[Union[str, "CloudSandbox"]]] = None,
        extend_s: Optional[int] = None,
    ) -> int:
        """Push back the lease of the matching leased sandboxes to now +
        ``extend_s`` (default: each one's own ``lease_s``). Returns how many.
        :func:`kosa8.rl.lease` does this in the background."""
        body = _selector(run_id, labels, ids)
        if extend_s is not None:
            _check_lease(extend_s)
            body["extend_s"] = extend_s
        return int((self._cloud._request("POST", "/v1/sandboxes/heartbeat", body) or {}).get("extended", 0))

    def drain(
        self,
        *,
        run_id: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ids: Optional[Sequence[Union[str, "CloudSandbox"]]] = None,
    ) -> Dict[str, Any]:
        """Delete every matching sandbox of the org: ``{"deleted": n, "ids":
        [...], "failed": [{"id", "code", "message", "failure_class",
        "retryable"}, ...]}``. Needs at least one of ``run_id``, ``labels`` or
        ``ids``. kosa8 Cloud deletes up to 1000 a call; this calls again while
        it says there are more (and stops if a call deleted none)."""
        sel = _selector(run_id, labels, ids)
        out: Dict[str, Any] = {"deleted": 0, "ids": [], "failed": []}
        for _ in range(_DRAIN_ROUNDS):
            r = self._cloud._request("POST", "/v1/sandboxes/drain", sel) or {}
            if not _drain_add(out, r):
                break
        return out

    def grade(
        self,
        sandbox: Union[str, "CloudSandbox"],
        cmd: Union[str, Sequence[str]],
        *,
        files: Optional[Mapping[str, Union[bytes, str]]] = None,
        timeout_s: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        keep: bool = False,
        run_id: Optional[str] = None,
        rollout: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> GradeResult:
        """Grade a solved sandbox in a sealed fork; see :meth:`CloudSandbox.grade`."""
        box = sandbox if isinstance(sandbox, CloudSandbox) else CloudSandbox(self._cloud, {"id": sandbox})
        return box.grade(cmd, files=files, timeout_s=timeout_s, env=env, keep=keep, run_id=run_id, rollout=rollout,
                         idempotency_key=idempotency_key)

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

    def __enter__(self) -> "CloudSandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        """``with cloud.sandboxes.create(...) as box:`` deletes it on the way out, error or not."""
        _quiet_delete(self)

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
        next one. kosa8 Cloud stops a buffered exec after 90 s, and
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
        return _exec_result(r)

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

    def fork(
        self,
        n: int = 1,
        *,
        labels: Optional[Mapping[str, str]] = None,
        run_id: Optional[str] = None,
        lease_s: Optional[int] = None,
        partial_ok: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> SandboxBatch:
        """``n`` live copies of the sandbox as it is now, on its host, as a
        :class:`SandboxBatch` (a list, with ``.failed``). Each is held to the
        rules of a create (payment, quotas).

        One API call makes up to 1000 (kosa8 Cloud makes them 100 at a time);
        for more this makes several calls, and if one of them raises, the
        copies already made are deleted. ``labels`` go on every copy,
        ``run_id`` labels them ``kosa8.run``, ``lease_s`` leases them (see
        :meth:`CloudSandboxes.create`). With ``partial_ok=True`` copies that
        could not be made (no room on the host, the running limit) are
        reported in ``.failed`` instead of failing the call; ``False`` is all
        or nothing. The API's default is ``True`` when ``labels``, ``run_id``
        or ``lease_s`` is given or ``n`` is over 100, else ``False``. A fork
        that made none raises the first refusal (its ``.body`` has
        ``requested``, ``created`` and ``failed``).

        Each call carries an ``Idempotency-Key`` its retries reuse
        (``idempotency_key`` is used for the first call; later ones add
        ``-2``, ``-3``, ...). A sandbox with secrets cannot be forked (409
        ``secrets``). May answer 501 ``not_supported`` on some hosts.
        """
        if not _is_int(n) or n < 1:
            raise ValueError("n must be a whole number of at least 1")
        amb = _ambient(self._cloud)
        run_id = amb.get("run_id") if run_id is None else run_id
        lease_s = amb.get("lease_s") if lease_s is None else lease_s
        _check_key(idempotency_key)
        out = SandboxBatch(requested=n, run_id=run_id)
        try:
            left, call = n, 0
            while left > 0:
                k = min(left, MAX_FORK_PER_CALL)
                body = _fork_body(k, labels, run_id, lease_s, partial_ok, prefix)
                r = self._cloud._request("POST", self._path("/fork"), body,
                                         idempotency_key=_chunk_key(idempotency_key, call)) or {}
                part = _batch_from(self._cloud, r, CloudSandbox, k)
                _reindex(part.failed, n - left)
                out.extend(part)
                out.failed.extend(part.failed)
                left -= k
                call += 1
        except BaseException:
            for child in out:
                _quiet_delete(child)
            raise
        return out

    def grade(
        self,
        cmd: Union[str, Sequence[str]],
        *,
        files: Optional[Mapping[str, Union[bytes, str]]] = None,
        timeout_s: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        keep: bool = False,
        run_id: Optional[str] = None,
        rollout: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> GradeResult:
        """Grade this sandbox's state with hidden tests, in a sealed fork.

        kosa8 Cloud forks one copy, denies it all network access, writes
        ``files`` (``{"/grader/test.py": b"..."}``; the agent's sandbox never
        sees them), runs ``cmd`` and deletes the copy (unless ``keep``). The
        reward is the last stdout line that is JSON with a numeric
        ``"reward"`` (``{"reward": 0.5, "metrics": {...}}``), else 1.0 for
        exit 0 and 0.0 otherwise; ``None`` (``failure_class`` ``timeout`` or
        ``infra``) when the command did not finish. With ``run_id`` kosa8
        Cloud records it as a rollout of that run (``rollout``: ``step``,
        ``group``, ``index``, ``meta``; ``.recorded`` says it did), so do not
        record it again.

        ``files``: at most 64, adding up to 8 MiB before base64 (a
        ``ValueError`` here otherwise). ``timeout_s``: 1 to 90 whole seconds.
        The call carries an ``Idempotency-Key`` (``idempotency_key``, default
        a new UUID) that its retries reuse: a retry after a timeout or a 5xx
        gets the first grade's answer, and records nothing twice.
        """
        amb = _ambient(self._cloud)
        run_id = amb.get("run_id") if run_id is None else run_id
        body = _grade_body(cmd, files=files, timeout_s=timeout_s, env=env, keep=keep, run_id=run_id, rollout=rollout)
        return _grade_result(self._cloud._request("POST", self._path("/grade"), body,
                                                  idempotency_key=idempotency_key) or {})

    def heartbeat(self, *, extend_s: Optional[int] = None) -> int:
        """Push back this sandbox's lease; see :meth:`CloudSandboxes.heartbeat`."""
        return self._cloud.sandboxes.heartbeat(ids=[self.id], extend_s=extend_s)

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


#: Drain calls at most (1000 sandboxes each).
_DRAIN_ROUNDS = 100


def _drain_add(out: Dict[str, Any], r: Mapping[str, Any]) -> bool:
    """Add one drain answer to ``out``; True if another call is worth making."""
    n = int(r.get("deleted", 0) or 0)
    out["deleted"] += n
    out["ids"].extend(r.get("ids") or [])
    out["failed"].extend(r.get("failed") or [])
    return bool(r.get("more")) and n > 0


def _chunk_key(key: Optional[str], call: int) -> Optional[str]:
    """The Idempotency-Key of a fork's ``call``-th request (``None``: a new one)."""
    if key is None or call == 0:
        return key
    suffix = f"-{call + 1}"
    return key[: 255 - len(suffix)] + suffix


def _reindex(failed: List[Dict[str, Any]], offset: int) -> None:
    """Make a later fork call's ``failed[i].index`` count from the first copy."""
    if offset:
        for f in failed:
            if _is_int(f.get("index")):
                f["index"] += offset


def _quiet_delete(box: Any) -> None:
    """Delete, ignoring a sandbox that is already gone or an API that will not
    answer (its lease or ttl then deletes it)."""
    try:
        box._cloud._request("DELETE", box._path())
    except Kosa8NotFoundError:
        pass
    except Exception:  # noqa: BLE001 (cleanup is best effort)
        pass


def _wait_all(batch: SandboxBatch, timeout: float, partial_ok: bool) -> None:
    """Poll every not-yet-running sandbox of ``batch`` until each settles."""
    deadline = time.monotonic() + timeout
    keep: List[CloudSandbox] = []
    try:
        for i, box in enumerate(list(batch)):
            try:
                if box.state not in _SETTLED:
                    box.wait_until_running(timeout=max(1.0, deadline - time.monotonic()))
                elif box.state == "error":
                    raise Kosa8Error(f"sandbox {box.id} failed to start: {box.info.get('last_error') or 'no reason'}")
                keep.append(box)
            except Kosa8Error as e:
                if not partial_ok:
                    raise
                _quiet_delete(box)
                fc, rt = classify_failure(e)
                batch.failed.append({"index": i, "id": box.id, "code": e.code or "not_ready", "message": str(e),
                                     "failure_class": "infra" if fc == "error" else fc, "retryable": True})
    except BaseException:
        for box in batch:
            _quiet_delete(box)
        raise
    batch[:] = keep


@dataclass
class ForkResult:
    """One item of :meth:`Kosa8Cloud.fork_map`."""

    item: Any
    #: What ``fn`` returned (``None`` when it failed).
    value: Any = None
    #: The exception of the last attempt, when it failed.
    error: Optional[BaseException] = None
    #: ``ok``, or the failure's class (``infra``, ``timeout``, ``quota``, ``client``, ``error``).
    failure_class: str = "ok"
    attempts: int = 0
    #: The child of the last attempt (already deleted).
    sandbox_id: Optional[str] = None
    ms: int = 0

    @property
    def ok(self) -> bool:
        return self.failure_class == "ok"
