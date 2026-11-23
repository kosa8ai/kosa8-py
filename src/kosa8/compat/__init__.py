# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Drop-in shims for code written against other sandbox SDKs.

Change one import and the same code runs on kosa8 Cloud::

    from e2b import Sandbox                       # before
    from kosa8.compat.e2b import Sandbox          # after

    from daytona import Daytona                   # before
    from kosa8.compat.daytona import Daytona      # after

    import modal                                  # before
    import kosa8.compat.modal as modal            # after

Every shim calls :class:`kosa8.Kosa8Cloud`: the key is ``KOSA8_API_KEY``, or
the other SDK's own variable (``E2B_API_KEY``, ``DAYTONA_API_KEY``) when it
holds a kosa8 key; ``KOSA8_CLOUD_URL`` for another address. A call kosa8
cannot do raises :class:`Unsupported`, which names the method and what to use
on kosa8 instead; a shim never pretends a call worked. With
``KOSA8_FALLBACK=e2b`` (``daytona``, ``modal``, ``all``) a *create* the shim
refuses runs on the original provider through its own SDK and key.

How the shims run things:

* Sandboxes are persistent, one long-lived machine as on E2B, Daytona and
  Modal: packages, files anywhere and background processes last between
  commands (each command is its own process, so ``cd`` and ``export`` do not
  carry). A plan with no persistent sandboxes left gets a plain one, where
  only ``/workspace`` lasts, with a warning; ``KOSA8_SHIM_PERSISTENT=0``
  skips the attempt. Relative paths are put under ``/workspace``.
* Every foreground command is a streamed exec: output reaches the other
  SDK's callbacks or iterators as it prints, and the command runs as long as
  that SDK's own timeout says, not kosa8's 90 s for a buffered exec.
* A background command (E2B ``background=True``, Modal's entrypoint) is
  polled about once a second; its new output is passed on as it appears,
  stdout and stderr together.

Every shim object keeps the kosa8 object it wraps as ``.kosa8``
(a :class:`kosa8.CloudSandbox`) for anything the other SDK has no word for.
"""

from __future__ import annotations

import functools
import os
import posixpath
import threading
import time
import urllib.parse
import warnings
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..cloud import CloudExecResult, CloudExecStream, CloudSandbox, Kosa8Cloud
from ..errors import Kosa8Error

__all__ = ["Unsupported", "set_default_cloud", "get_cloud", "env_key", "create_sandbox", "fallback_enabled", "StreamPump"]


class Unsupported(Kosa8Error, NotImplementedError):
    """A method of another SDK that kosa8 cannot do.

    ``method`` is the method as the other SDK spells it (``e2b.Sandbox.pty``),
    ``alternative`` what to use on kosa8 instead.
    """

    def __init__(self, method: str, alternative: str, reason: str = "") -> None:
        why = f": {reason}" if reason else ""
        super().__init__(f"{method} is not supported by kosa8{why}. On kosa8: {alternative}", code="unsupported")
        self.method = method
        self.alternative = alternative


_default_cloud: Optional[Kosa8Cloud] = None


def set_default_cloud(cloud: Optional[Kosa8Cloud]) -> None:
    """Make every shim use ``cloud`` (for a custom address, timeout or
    ``opener``); ``None`` goes back to a ``Kosa8Cloud()`` per call."""
    global _default_cloud
    _default_cloud = cloud


KEY_PREFIXES = ("k8c_live_", "k8c_tok_")


def env_key(*names: str) -> Optional[str]:
    """The key to use when the caller passed none: ``KOSA8_API_KEY``, else the
    first of the other SDK's own variables (``E2B_API_KEY``,
    ``DAYTONA_API_KEY``, ...) that holds a kosa8 key. A variable holding the
    other provider's key is left alone (``KOSA8_FALLBACK`` may use it)."""
    k = os.environ.get("KOSA8_API_KEY", "").strip()
    if k:
        return k
    for n in names:
        v = os.environ.get(n, "").strip()
        if v.startswith(KEY_PREFIXES):
            return v
    return None


def get_cloud(api_key: Optional[str] = None, *, key_env: Sequence[str] = (), base_url: Optional[str] = None) -> Kosa8Cloud:
    """The cloud a shim talks to: the one given to :func:`set_default_cloud`,
    else ``Kosa8Cloud(api_key)``. With no ``api_key``, :func:`env_key` picks
    it (``KOSA8_API_KEY``, then the ``key_env`` variables holding a kosa8
    key). ``base_url`` is a kosa8 address the other SDK's own setting named
    (E2B's ``E2B_DOMAIN=kosa8.com``)."""
    if api_key is None and _default_cloud is None:
        api_key = env_key(*key_env)
    if _default_cloud is not None and api_key is None and base_url is None:
        return _default_cloud
    if _default_cloud is not None:
        return Kosa8Cloud(api_key if api_key is not None else _default_cloud._key,
                          base_url=base_url or _default_cloud.base_url, timeout=_default_cloud.timeout,
                          opener=_default_cloud._open, stream_opener=_default_cloud._stream_open)
    return Kosa8Cloud(api_key, base_url=base_url)


def shim_persistent() -> bool:
    """Whether shims create persistent sandboxes (``KOSA8_SHIM_PERSISTENT=0`` turns it off)."""
    return os.environ.get("KOSA8_SHIM_PERSISTENT", "1").strip().lower() not in ("0", "false", "no", "off")


def _persistent_refused(e: Kosa8Error) -> bool:
    body = e.body if isinstance(e.body, dict) else {}
    if body.get("limit_name") == "persistent":
        return True
    return e.status in (400, 402, 403, 409) and "persistent" in str(e).lower()


def create_sandbox(cloud: Kosa8Cloud, *, persistent: bool = True, labels: Optional[Mapping[str, str]] = None,
                   **body: Any) -> CloudSandbox:
    """Create the sandbox a shim stands for. E2B, Daytona, Modal and Vercel
    all keep one machine alive across commands, so a shim asks for a
    persistent sandbox (installed packages, files outside /workspace and
    background processes last). When the plan allows no more persistent
    sandboxes it warns and creates a plain one (only /workspace lasts)."""
    if persistent and shim_persistent() and "snapshot" not in body and not body.get("docker"):
        try:
            return cloud.sandboxes.create(labels=labels or None, persistent=True, **body)
        except Kosa8Error as e:
            if not _persistent_refused(e):
                raise
            warnings.warn(
                f"kosa8: a persistent sandbox was refused ({e}); this one keeps only /workspace between commands. "
                "Set KOSA8_SHIM_PERSISTENT=0 to skip the attempt.", RuntimeWarning, stacklevel=3)
    return cloud.sandboxes.create(labels=labels or None, **body)


def fit_size(cpus: float, mem_mb: int) -> Tuple[Any, int]:
    """The smallest kosa8 size (``CLOUD_SIZES``) with at least ``cpus`` vCPU and
    ``mem_mb`` memory, as ``(cpus, mem_mb)``: other SDKs take any pair, kosa8
    takes fixed sizes, and more than asked is the safe way to round."""
    from ..cloud import CLOUD_SIZES

    for size in CLOUD_SIZES:
        c, g = (float(x) for x in size.split("x"))
        if c >= float(cpus) and g * 1024 >= mem_mb:
            return (int(c) if c >= 1 else c), int(g * 1024)
    return cpus, mem_mb  # bigger than the largest size: let the API say so


# --- KOSA8_FALLBACK: send what kosa8 cannot do back to the original provider ---------


def fallback_enabled(provider: str) -> bool:
    """``KOSA8_FALLBACK`` names ``provider`` (a comma list: ``e2b,daytona,modal``, or ``all``)."""
    v = {x.strip().lower() for x in os.environ.get("KOSA8_FALLBACK", "").split(",") if x.strip()}
    return provider in v or "all" in v


def fallback(provider: str, err: Unsupported, have_key: Callable[[], bool], run: Callable[[], Any]) -> Any:
    """With ``KOSA8_FALLBACK`` naming ``provider``, its SDK installed and its own
    key present, run ``run()`` (the same call on the original SDK) and warn;
    otherwise raise ``err``. Only calls that make a sandbox (create) fall
    back: a sandbox already on kosa8 stays there."""
    if not fallback_enabled(provider):
        raise err
    try:
        ok = have_key()
    except ImportError:
        ok = False
    if not ok:
        raise Unsupported(err.method, err.alternative,
                          f"KOSA8_FALLBACK={provider} is set, but the {provider} SDK or its own API key is missing") from err
    warnings.warn(f"kosa8: {err.method} is not supported by kosa8; KOSA8_FALLBACK={provider} runs it on {provider}",
                  RuntimeWarning, stacklevel=3)
    return run()


def foreign_key(name: str) -> bool:
    """``name`` is set and holds the other provider's key (not a kosa8 one)."""
    v = os.environ.get(name, "").strip()
    return bool(v) and not v.startswith(KEY_PREFIXES)


# --- helpers the shims share ---------------------------------------------------

WORKSPACE = "/workspace"


def abs_path(path: str) -> str:
    """A relative path is under /workspace, the folder kosa8 keeps between commands."""
    if not path:
        raise ValueError("a path is required")
    if path.startswith("/"):
        return path
    p = path[2:] if path.startswith("./") else path
    return posixpath.join(WORKSPACE, p)


def in_dir(argv: Sequence[str], cwd: Optional[str]) -> List[str]:
    """``argv`` run in ``cwd`` (relative to /workspace)."""
    if not cwd:
        return list(argv)
    return ["sh", "-c", 'cd "$1" || exit 1; shift; exec "$@"', "sh", abs_path(cwd), *argv]


def bash_or_sh(cmd: str) -> List[str]:
    """``cmd`` through bash when the image has it, else sh (the base template is Alpine)."""
    return ["sh", "-c", 'if command -v bash >/dev/null 2>&1; then exec bash -l -c "$1"; fi; exec sh -c "$1"', "sh", cmd]


def is_kosa8_url(url: str) -> bool:
    host = urllib.parse.urlparse(url if "//" in url else "https://" + url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host == "kosa8.com" or host.endswith(".kosa8.com")


def check_url(method: str, url: Optional[str]) -> None:
    """Refuse another provider's address rather than send a kosa8 key there."""
    if url and not is_kosa8_url(url):
        raise Unsupported(
            method,
            "leave it out; kosa8 reads KOSA8_CLOUD_URL (default https://api.kosa8.com)",
            f"{url} is not a kosa8 address and the kosa8 API key is never sent to it",
        )


def given(v: Any) -> bool:
    """An argument the caller set: not None, False or empty (0 counts as set)."""
    if v is None or v is False:
        return False
    return not (isinstance(v, (list, tuple, dict, set, str)) and len(v) == 0)


def stat_entry(box: Any, path: str) -> Optional[Dict[str, Any]]:
    """One file's entry from its folder's listing, or None when it is not there."""
    p = abs_path(path).rstrip("/") or "/"
    if p == "/":
        return {"name": "/", "path": "/", "type": "dir"}
    parent, name = posixpath.split(p)
    from ..errors import Kosa8NotFoundError

    try:
        entries = box.files.list(parent or "/")
    except Kosa8NotFoundError:
        return None
    for e in entries:
        if e.get("name") == name or e.get("path") == p:
            return e
    return None


class Hybrid:
    """A method callable on the class (with an id first) and on an instance,
    the way E2B's ``Sandbox.kill(sandbox_id)`` and ``sandbox.kill()`` both work."""

    def __init__(self, on_instance: Callable[..., Any], on_class: Callable[..., Any]) -> None:
        self._inst = on_instance
        self._cls = on_class
        functools.update_wrapper(self, on_instance)

    def __get__(self, obj: Any, typ: Optional[type] = None) -> Callable[..., Any]:
        if obj is None:
            return functools.partial(self._cls, typ)
        return functools.partial(self._inst, obj)


def translating(convert: Callable[[Kosa8Error], Optional[BaseException]]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Raise the other SDK's exception for a kosa8 one (the kosa8 error is its
    ``__cause__``). :class:`Unsupported` goes through as it is."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*a: Any, **kw: Any) -> Any:
            try:
                return fn(*a, **kw)
            except Unsupported:
                raise
            except Kosa8Error as e:
                other = convert(e)
                if other is None:
                    raise
                raise other from e

        return run

    return deco



class StreamPump:
    """A streamed exec read to its end on a thread, for APIs that hand back a
    process while it still runs (Modal's ``exec``, Daytona's async session
    commands). ``on_chunk(stream, text)`` is called on that thread as output
    arrives; :meth:`follow` yields the chunks to any number of readers, and
    :meth:`wait` gives the :class:`kosa8.CloudExecResult` (or raises what the
    stream raised)."""

    def __init__(self, stream: CloudExecStream, on_chunk: Optional[Callable[[str, str], None]] = None) -> None:
        self._stream = stream
        self._on_chunk = on_chunk
        self._cv = threading.Condition()
        self.chunks: List[Tuple[str, str]] = []
        self.done = False
        self.error: Optional[BaseException] = None
        self.result: Optional[CloudExecResult] = None
        self._thread = threading.Thread(target=self._run, name="kosa8-exec-stream", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for name, text in self._stream:
                if self._on_chunk is not None:
                    self._on_chunk(name, text)
                with self._cv:
                    self.chunks.append((name, text))
                    self._cv.notify_all()
            code = self._stream.exit_code
            self.result = CloudExecResult(exit_code=code, stdout=self._stream.stdout, stderr=self._stream.stderr,
                                          failure_class=None if code is None else ("ok" if code == 0 else "exit_nonzero"))
        except BaseException as e:  # noqa: BLE001 (handed to whoever waits)
            self.error = e
        finally:
            self._stream.close()
            with self._cv:
                self.done = True
                self._cv.notify_all()

    def follow(self, start: int = 0) -> Iterator[Tuple[str, str]]:
        """Every chunk from index ``start`` on, blocking for new ones until the command ends."""
        i = start
        while True:
            with self._cv:
                while i >= len(self.chunks) and not self.done:
                    self._cv.wait()
                batch = self.chunks[i:]
                finished = self.done
            yield from batch
            i += len(batch)
            if finished and i >= len(self.chunks):
                return

    def text(self, stream: Optional[str] = None) -> str:
        """The output so far: one stream, or both interleaved as printed."""
        with self._cv:
            return "".join(t for n, t in self.chunks if stream is None or n == stream)

    def wait(self, timeout: Optional[float] = None) -> CloudExecResult:
        self._thread.join(timeout)
        if not self.done:
            raise TimeoutError("the command is still running")
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def close(self) -> None:
        """Stop reading (closes the connection)."""
        self._stream.close()


def poll_output(status: Callable[[], Dict[str, Any]], interval: float = 1.0) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Follow a background command: yields ``(new output, status)`` each time
    its output grows, and a last ``("", status)`` once it has exited."""
    seen = 0
    while True:
        s = status()
        out = s.get("output") or ""
        new = out[seen:] if len(out) > seen else ""
        seen = max(seen, len(out))
        exited = s.get("state") == "exited"
        if new:
            yield new, s
        if exited:
            yield "", s
            return
        time.sleep(interval)
