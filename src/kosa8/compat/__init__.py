# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Drop-in shims for code written against other sandbox SDKs.

Change one import and the same code runs on kosa8 Cloud::

    from e2b import Sandbox                       # before
    from kosa8.compat.e2b import Sandbox          # after

    from daytona import Daytona                   # before
    from kosa8.compat.daytona import Daytona      # after

    import modal                                  # before
    import kosa8.compat.modal as modal            # after

Every shim calls :class:`kosa8.Kosa8Cloud` (``KOSA8_API_KEY`` from the
environment, ``KOSA8_CLOUD_URL`` for another address). A call kosa8 cannot do
raises :class:`Unsupported`, which names the method and what to use on kosa8
instead; a shim never pretends a call worked.

Things that differ everywhere, because kosa8 Cloud works this way:

* Each command runs in a fresh container and only ``/workspace`` persists
  between commands, so relative paths are put under ``/workspace``.
* Output streams where the other SDK streams it. A command given output
  callbacks (E2B ``on_stdout`` / ``on_stderr``), a timeout over 90 s, or run
  through an API that is itself a stream (Modal ``sandbox.exec(...)``,
  Daytona session commands and their ``get_session_command_logs_async``)
  is a streamed exec: the output arrives as the command prints it and the
  command is not held to 90 s. A plain buffered command (no callbacks, no
  long timeout) still runs at most 90 s and answers once, when it ends.
* A background command (E2B ``background=True``, Modal's entrypoint) is
  polled about once a second; its new output is passed on as it appears,
  stdout and stderr together.

Every shim object keeps the kosa8 object it wraps as ``.kosa8``
(a :class:`kosa8.CloudSandbox`) for anything the other SDK has no word for.
"""

from __future__ import annotations

import functools
import posixpath
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..cloud import CloudExecResult, CloudExecStream, Kosa8Cloud
from ..errors import Kosa8Error

__all__ = ["Unsupported", "set_default_cloud", "get_cloud", "StreamPump"]


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


def get_cloud(api_key: Optional[str] = None) -> Kosa8Cloud:
    """The cloud a shim talks to: the one given to :func:`set_default_cloud`,
    else ``Kosa8Cloud(api_key)`` (``KOSA8_API_KEY`` when ``api_key`` is None)."""
    if _default_cloud is not None and api_key is None:
        return _default_cloud
    if _default_cloud is not None:
        return Kosa8Cloud(api_key, base_url=_default_cloud.base_url, timeout=_default_cloud.timeout,
                          opener=_default_cloud._open, stream_opener=_default_cloud._stream_open)
    return Kosa8Cloud(api_key)


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
