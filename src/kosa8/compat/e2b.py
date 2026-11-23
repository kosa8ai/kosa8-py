# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""E2B's ``Sandbox`` on kosa8 Cloud: change ``from e2b import Sandbox`` to
``from kosa8.compat.e2b import Sandbox`` (and ``from e2b_code_interpreter
import Sandbox`` to ``from kosa8.compat.e2b_code_interpreter import Sandbox``).

Mirrors the synchronous ``Sandbox`` of the ``e2b`` Python SDK **2.64.0** and
``e2b-code-interpreter`` **2.11.10**, from their public references:
https://docs.e2b.dev/sdk-reference/python-sdk/v2.64.0/sandbox_sync and
https://docs.e2b.dev/sdk-reference/code-interpreter-python-sdk/v2.11.10/code_interpreter
(checked 2026-11-21). Async (``AsyncSandbox``) is not mirrored.

The key is a kosa8 Cloud key: ``KOSA8_API_KEY``, ``api_key=`` with a
``k8c_live_`` key, or ``E2B_API_KEY`` when it holds one (an E2B key there is
never sent to kosa8). ``E2B_DOMAIN=kosa8.com`` (or ``domain=``) means
``https://api.kosa8.com``; another provider's ``E2B_DOMAIN`` is ignored.

What maps, and how:

=============================  ==============================================
E2B                            kosa8 Cloud
=============================  ==============================================
``Sandbox.create``             ``cloud.sandboxes.create``; ``timeout`` is the
                               sandbox's ``ttl_s`` (60 s at least),
                               ``envs`` its env, ``metadata`` its labels,
                               ``allow_internet_access=False`` no internet,
                               ``lifecycle={"on_timeout": "pause"}`` sleep
                               after ``timeout``. A ``snap_...`` template
                               starts from that kosa8 snapshot. Persistent,
                               like an E2B sandbox; ``network`` allow/deny
                               lists are kosa8 egress.
``connect``                    get; wakes a sleeping sandbox
``kill``                       delete
``set_timeout``                a new ``ttl_s``, from now
``pause`` / ``beta_pause``     sleep (memory and disk kept)
``fork``                       ``fork(n=count)``
``create_snapshot``            a durable snapshot
``list_snapshots`` /           ``cloud.snapshots``
``delete_snapshot``
``commands.run``               a streamed exec (``bash -l -c`` when the
                               image has bash, else ``sh -c``), held to E2B's
                               ``timeout``, not 90 s; ``background=True`` is a
                               background exec
``get_host``                   a public kosa8 preview's host
``update_network``             the sandbox's egress policy, replaced
``files.*``                    the sandbox's files API; relative paths are
                               under E2B's home, /home/user (/workspace on a
                               sandbox that could not be persistent), where
                               commands start too
``run_code`` (interpreter)     the code interpreter (Python only); the
                               code-interpreter ``Sandbox`` is created
                               persistent, which the interpreter needs
=============================  ==============================================

Everything else raises :class:`kosa8.compat.Unsupported`. Differences to know:
``on_stdout``/``on_stderr`` are called as the command prints. A background
command's output is polled about once a second, and its stdout and stderr
come back together, as stdout. ``run_code`` output arrives when the cell ends.
"""

from __future__ import annotations

import datetime as _dt
import enum
import io
import math
import os
import posixpath
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from ..cloud import MAX_TTL_S, MIN_TTL_S, CloudSandbox, Kosa8Cloud
from ..cloud_features import CloudBackgroundExec
from ..errors import (
    Kosa8AuthError,
    Kosa8BadRequestError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
)
from . import (
    Hybrid, Unsupported, bash_or_sh, check_url, create_sandbox, fallback, foreign_key, get_cloud, in_dir,
    is_kosa8_url, poll_output, stat_entry, translating,
)

__all__ = [
    "Sandbox", "Commands", "Filesystem", "CommandResult", "CommandHandle", "EntryInfo", "WriteInfo", "FileType",
    "SandboxInfo", "SnapshotInfo", "SandboxPaginator", "SnapshotPaginator", "Execution", "Result", "Logs",
    "ExecutionError", "OutputMessage", "Context",
    "SandboxException", "TimeoutException", "InvalidArgumentException", "NotFoundException",
    "FileNotFoundException", "SandboxNotFoundException", "AuthenticationException", "RateLimitException",
    "CommandExitException", "Unsupported",
]

#: The E2B SDK versions this module mirrors.
MIRRORS = {"e2b": "2.64.0", "e2b-code-interpreter": "2.11.10"}

_E2B_DEFAULT_TIMEOUT = 300


# --- exceptions, as e2b.exceptions names them ------------------------------------


class SandboxException(Exception):
    """Base class of E2B's sandbox errors."""


class TimeoutException(SandboxException):
    pass


class InvalidArgumentException(SandboxException):
    pass


class NotFoundException(SandboxException):
    pass


class FileNotFoundException(NotFoundException):
    pass


class SandboxNotFoundException(NotFoundException):
    pass


class AuthenticationException(Exception):
    pass


class RateLimitException(SandboxException):
    pass


def _convert(e: Kosa8Error) -> BaseException:
    msg = str(e)
    if isinstance(e, (Kosa8ExecTimeoutError, Kosa8TimeoutError)):
        return TimeoutException(msg)
    if isinstance(e, Kosa8NotFoundError):
        return NotFoundException(msg)
    if isinstance(e, Kosa8AuthError):
        return AuthenticationException(msg)
    if isinstance(e, Kosa8RateLimitError):
        return RateLimitException(msg)
    if isinstance(e, Kosa8BadRequestError):
        return InvalidArgumentException(msg)
    return SandboxException(msg)


_e2b = translating(_convert)


# --- results -------------------------------------------------------------------------


@dataclass
class CommandResult:
    stderr: Optional[str]
    stdout: Optional[str]
    exit_code: Optional[int]
    error: Optional[str] = None


class CommandExitException(SandboxException, CommandResult):
    """A foreground command exited non-zero (E2B raises; so does this)."""

    def __init__(self, stderr: str, stdout: str, exit_code: Optional[int], error: Optional[str] = None) -> None:
        CommandResult.__init__(self, stderr=stderr, stdout=stdout, exit_code=exit_code, error=error)
        SandboxException.__init__(self, f"Command exited with code {exit_code} and error:\n{stderr}")


class FileType(enum.Enum):
    FILE = "file"
    DIR = "dir"


@dataclass
class EntryInfo:
    name: str
    type: Optional[FileType]
    path: str
    size: int = 0
    mode: int = 0
    permissions: str = ""
    owner: str = ""
    group: str = ""
    modified_time: Optional[_dt.datetime] = None
    symlink_target: Optional[str] = None


@dataclass
class WriteInfo:
    name: str
    type: Optional[FileType]
    path: str


@dataclass
class SandboxInfo:
    sandbox_id: str
    template_id: str
    name: Optional[str]
    metadata: Dict[str, str]
    started_at: Optional[_dt.datetime]
    end_at: Optional[_dt.datetime]
    state: str
    cpu_count: Optional[int] = None
    memory_mb: Optional[int] = None


@dataclass
class SnapshotInfo:
    snapshot_id: str
    name: Optional[str] = None
    sandbox_id: Optional[str] = None
    state: Optional[str] = None

    @property
    def names(self) -> List[str]:
        """E2B lists every name a snapshot has; a kosa8 snapshot has one at most."""
        return [self.name] if self.name else []


class _OnePage:
    """E2B's paginators; kosa8 answers everything in one page here."""

    def __init__(self, fetch: Callable[[], List[Any]]) -> None:
        self._fetch = fetch
        self._done = False

    @property
    def has_next(self) -> bool:
        return not self._done

    @property
    def next_token(self) -> Optional[str]:
        return None

    def next_items(self) -> List[Any]:
        if self._done:
            raise SandboxException("No more items to fetch")
        self._done = True
        return self._fetch()


class SandboxPaginator(_OnePage):
    pass


class SnapshotPaginator(_OnePage):
    pass


def _time(v: Any) -> Optional[_dt.datetime]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _dt.datetime.fromtimestamp(v / 1000, tz=_dt.timezone.utc)
    if isinstance(v, str) and v:
        try:
            return _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _entry(e: Mapping[str, Any]) -> EntryInfo:
    t = e.get("type")
    mode = e.get("mode")
    return EntryInfo(
        name=e.get("name") or posixpath.basename(e.get("path", "")),
        type=FileType.DIR if t == "dir" else FileType.FILE if t else None,
        path=e.get("path") or "",
        size=int(e.get("size") or 0),
        mode=mode if isinstance(mode, int) else 0,
        permissions=mode if isinstance(mode, str) else "",
        modified_time=_time(e.get("mod_time")),
    )


def _info(s: Mapping[str, Any]) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=s["id"],
        template_id=s.get("template") or "base",
        name=s.get("name"),
        metadata=dict(s.get("labels") or {}),
        started_at=_time(s.get("started_at") or s.get("created_at")),
        end_at=_time(s.get("delete_after")),
        state="paused" if s.get("state") == "sleeping" else "running" if s.get("state") == "running" else str(s.get("state")),
        cpu_count=s.get("cpus"),
        memory_mb=s.get("mem_mb"),
    )


def _ttl(timeout: Optional[int]) -> int:
    t = _E2B_DEFAULT_TIMEOUT if timeout is None else int(timeout)
    return min(max(t, MIN_TTL_S), MAX_TTL_S)


def _refuse_user(method: str, user: Any) -> None:
    if user is not None:
        raise Unsupported(f"e2b {method}(user=...)", "commands run as the image's user; switch user inside the command (su, sudo)")


# --- the working directory --------------------------------------------------------------

#: E2B's user's home: relative paths and commands start there, as in E2B.
HOME = "/home/user"


def _home(box: CloudSandbox) -> str:
    """Where relative paths go and commands start: E2B's ``/home/user`` on a
    persistent sandbox, ``/workspace`` (the folder kosa8 keeps between
    commands) on a plain one."""
    return HOME if box.info.get("persistent") else "/workspace"


def _abs(box: CloudSandbox, path: str) -> str:
    if not path:
        raise InvalidArgumentException("a path is required")
    if path.startswith("/"):
        return path
    p = path[2:] if path.startswith("./") else path
    return posixpath.join(_home(box), p)


def _in_home(box: CloudSandbox, argv: List[str], cwd: Optional[str]) -> List[str]:
    """``argv`` run in ``cwd`` (relative to the home), or in the home; the home
    is made on first use."""
    d = _abs(box, cwd) if cwd else _home(box)
    mk = 'mkdir -p "$1" 2>/dev/null; ' if not cwd else ""
    return ["sh", "-c", mk + 'cd "$1" || exit 1; shift; exec "$@"', "sh", d, *argv]


# --- commands --------------------------------------------------------------------------


class CommandHandle:
    """A background command (``commands.run(..., background=True)``).

    ``pid`` is kosa8's background command id (``bg_...``), not a process id.
    Its stdout and stderr are recorded together, and come back as stdout.
    Output is followed by polling the command about once a second
    (:attr:`poll_interval`): ``on_stdout`` given to ``run`` is called from a
    thread with each new piece as it appears, ``wait(on_stdout=...)`` with
    each new piece while it waits, and iterating yields them.
    """

    #: Seconds between polls of a background command's output.
    poll_interval = 1.0

    def __init__(self, job: CloudBackgroundExec, on_stdout: Optional[Callable[[str], None]] = None,
                 on_stderr: Optional[Callable[[str], None]] = None, retain_output: bool = True) -> None:
        self._job = job
        self._retain = retain_output
        self._result: Optional[CommandResult] = None
        self._out: List[str] = []
        self._final: Optional[Dict[str, Any]] = None
        self._listeners: List[Callable[[str], None]] = [on_stdout] if on_stdout is not None else []
        self._lock = threading.Lock()
        self._error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        if self._listeners:
            # E2B calls run()'s callbacks as output arrives, without wait().
            self._thread = threading.Thread(target=self._pump_quietly, name="kosa8-e2b-bg", daemon=True)
            self._thread.start()

    @property
    def pid(self) -> str:
        return self._job.id

    def _pump(self) -> Iterator[str]:
        """New output as it appears, until the command exits (one reader at a time)."""
        for new, s in poll_output(self._job.status, self.poll_interval):
            if new:
                with self._lock:
                    self._out.append(new)
                    listeners = list(self._listeners)
                for cb in listeners:
                    cb(new)
                yield new
            else:
                self._final = s

    def _pump_quietly(self) -> None:
        try:
            for _ in self._pump():
                pass
        except BaseException as e:  # noqa: BLE001 (raised again by wait)
            self._error = e

    def _finish(self) -> CommandResult:
        if self._error is not None:
            raise self._error
        done = self._final or {}
        out = "".join(self._out) if self._retain else None
        code = done.get("exit")
        if code not in (0, None):
            raise CommandExitException(stderr="", stdout=out or "", exit_code=code)
        self._result = CommandResult(stderr="" if self._retain else None, stdout=out, exit_code=code)
        return self._result

    @_e2b
    def wait(self, on_pty: Optional[Callable[[Any], None]] = None, on_stdout: Optional[Callable[[str], None]] = None,
             on_stderr: Optional[Callable[[str], None]] = None) -> CommandResult:
        """Until the command exits; ``on_stdout`` gets its output as it appears
        (what it printed before this call first, in one piece)."""
        if on_pty is not None:
            raise Unsupported("e2b CommandHandle.wait(on_pty=...)", "sandbox.kosa8.terminal() for an interactive terminal")
        if self._result is not None and self._thread is None:
            if on_stdout and self._result.stdout:
                on_stdout(self._result.stdout)
            return self._result
        if self._thread is not None:
            with self._lock:
                before = "".join(self._out)
                if on_stdout is not None:
                    self._listeners.append(on_stdout)
            if on_stdout and before:
                on_stdout(before)
            self._thread.join()
            self._thread = None
            return self._finish()
        if on_stdout is not None:
            self._listeners.append(on_stdout)
        for _ in self._pump():
            pass
        return self._finish()

    def __iter__(self) -> Iterator[Tuple[Optional[str], Optional[str], None]]:
        """Yields ``(stdout, None, None)`` for each new piece of output as it appears."""
        if self._result is not None or self._thread is not None:
            r = self.wait() if self._result is None else self._result
            yield (r.stdout, None, None)
            return
        for new in self._pump():
            yield (new, None, None)
        self._finish()

    def disconnect(self) -> None:
        """Stop following it here; the command keeps running."""

    @_e2b
    def kill(self) -> bool:
        return _stop(self._job)

    def send_stdin(self, data: str, request_timeout: Optional[float] = None) -> None:
        raise Unsupported("e2b CommandHandle.send_stdin", "kosa8 commands take no stdin; write the input to a file in /workspace and read it")

    def close_stdin(self, request_timeout: Optional[float] = None) -> None:
        raise Unsupported("e2b CommandHandle.close_stdin", "kosa8 commands take no stdin")


def _stop(job: CloudBackgroundExec) -> bool:
    try:
        job._cloud._request("POST", f"{job._base}/exec/{job.id}/stop", {})
    except Kosa8NotFoundError:
        return False
    return True


class Commands:
    """``sandbox.commands``."""

    def __init__(self, box: CloudSandbox) -> None:
        self._box = box

    @_e2b
    def run(
        self,
        cmd: str,
        background: Optional[bool] = None,
        envs: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        cwd: Optional[str] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
        stdin: Optional[bool] = None,
        timeout: Optional[float] = 60,
        request_timeout: Optional[float] = None,
        *,
        retain_output: bool = True,
        shell: Optional[Sequence[str]] = None,
    ) -> Union[CommandResult, CommandHandle]:
        """Run ``cmd`` and wait (or start it with ``background=True``).

        Non-zero exit raises :class:`CommandExitException`, as in E2B.

        A foreground command is a streamed exec: ``on_stdout`` / ``on_stderr``
        get the output as it prints, and ``timeout`` (60 s by default, 0 for
        no limit) is applied in the sandbox (:class:`TimeoutException` past
        it); kosa8's 90 s cap on a buffered exec does not apply. With
        ``background=True`` the callbacks are called from a thread as output
        appears (polled about once a second, stdout and stderr together).
        """
        _refuse_user("Commands.run", user)
        if stdin:
            raise Unsupported("e2b Commands.run(stdin=True)", "kosa8 commands take no stdin; write the input to a file in /workspace")
        if shell is not None:
            if isinstance(shell, (str, set)) or not isinstance(shell, Sequence) or not shell or not shell[0]:
                raise InvalidArgumentException("shell must be a sequence such as ['/bin/sh', '-c']")
            argv = [*shell, cmd]
        else:
            argv = bash_or_sh(cmd)
        argv = _in_home(self._box, argv, cwd)
        if background:
            return CommandHandle(self._box.exec_background(argv, env=envs), on_stdout, on_stderr, retain_output=retain_output)
        # Always a streamed exec: output reaches on_stdout/on_stderr as it
        # prints, and the command runs as long as E2B's timeout says (60 s by
        # default, 0 for no limit), not kosa8's 90 s for a buffered exec.
        limit = None if timeout is None or timeout <= 0 else max(1, int(math.ceil(timeout)))
        r = self._box.exec_stream(argv, env=envs, timeout_s=limit).result(on_stdout=on_stdout, on_stderr=on_stderr)
        out = r.stdout if retain_output else None
        err = r.stderr if retain_output else None
        if r.exit_code not in (0, None):
            raise CommandExitException(stderr=err or "", stdout=out or "", exit_code=r.exit_code)
        return CommandResult(stderr=err, stdout=out, exit_code=r.exit_code)

    @_e2b
    def connect(self, pid: str, timeout: Optional[float] = 60, request_timeout: Optional[float] = None,
                on_stdout: Optional[Callable[[str], None]] = None, on_stderr: Optional[Callable[[str], None]] = None,
                *, retain_output: bool = True) -> CommandHandle:
        """A background command by its id (``bg_...``)."""
        return CommandHandle(self._box.get_exec(str(pid)), on_stdout, on_stderr, retain_output=retain_output)

    @_e2b
    def kill(self, pid: str, request_timeout: Optional[float] = None) -> bool:
        """Stop a background command; False when there is no such command."""
        return _stop(CloudBackgroundExec(self._box._cloud, self._box._path(), {"id": str(pid)}))

    def list(self, request_timeout: Optional[float] = None) -> List[Any]:  # noqa: A003
        raise Unsupported("e2b Commands.list", "keep the CommandHandle (or its pid) that run(background=True) returned")

    def send_stdin(self, pid: Any, data: str, request_timeout: Optional[float] = None) -> None:
        raise Unsupported("e2b Commands.send_stdin", "kosa8 commands take no stdin; write the input to a file in /workspace")

    def close_stdin(self, pid: Any, request_timeout: Optional[float] = None) -> None:
        raise Unsupported("e2b Commands.close_stdin", "kosa8 commands take no stdin")

    def stream_input(self, *a: Any, **kw: Any) -> None:
        raise Unsupported("e2b Commands.stream_input", "kosa8 commands take no stdin")


# --- files --------------------------------------------------------------------------------


@dataclass
class WriteEntry:
    path: str
    data: Union[str, bytes, io.IOBase]


def _bytes(data: Any) -> bytes:
    if isinstance(data, str):
        return data.encode()
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if hasattr(data, "read"):
        v = data.read()
        return v.encode() if isinstance(v, str) else bytes(v)
    if hasattr(data, "__iter__"):
        out = bytearray()
        for chunk in data:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise InvalidArgumentException(f"an iterable written to a file must yield bytes, not {type(chunk).__name__}")
            out += chunk
        return bytes(out)
    raise InvalidArgumentException(f"cannot write {type(data).__name__}: give str, bytes, a file object or an iterable of bytes")


class _ByteStream:
    """``files.read(format="stream")``: an iterator of bytes that is also a
    context manager with ``close()``, as E2B's is. kosa8 reads the file in one
    piece, so it yields it in chunks of 64 KiB."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._i = 0

    def __iter__(self) -> "_ByteStream":
        return self

    def __next__(self) -> bytes:
        if self._i >= len(self._data):
            raise StopIteration
        chunk = self._data[self._i:self._i + 65536]
        self._i += len(chunk)
        return chunk

    def close(self) -> None:
        self._i = len(self._data)

    def __enter__(self) -> "_ByteStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Filesystem:
    """``sandbox.files``. Relative paths are under /workspace, and only
    /workspace outlives a command."""

    def __init__(self, box: CloudSandbox) -> None:
        self._box = box

    @_e2b
    def read(self, path: str, format: str = "text", user: Optional[str] = None,  # noqa: A002 (E2B's name)
             request_timeout: Optional[float] = None, gzip: bool = False, stream_idle_timeout: Optional[float] = None) -> Any:
        """``format``: ``text`` (str), ``bytes`` (bytearray) or ``stream`` (an iterator of bytes)."""
        _refuse_user("Filesystem.read", user)
        try:
            data = self._box.files.read(_abs(self._box, path))
        except Kosa8NotFoundError as e:
            raise FileNotFoundException(str(e)) from e
        if format == "text":
            return data.decode("utf-8")
        if format == "bytes":
            return bytearray(data)
        if format == "stream":
            return _ByteStream(data)
        raise InvalidArgumentException(f"format must be text, bytes or stream, not {format!r}")

    @_e2b
    def write(self, path: str, data: Any, user: Optional[str] = None, request_timeout: Optional[float] = None,
              gzip: bool = False, use_octet_stream: Optional[bool] = None, metadata: Optional[Dict[str, str]] = None) -> WriteInfo:
        _refuse_user("Filesystem.write", user)
        if metadata:
            raise Unsupported("e2b Filesystem.write(metadata=...)", "kosa8 files carry no metadata; keep it in a sidecar file")
        p = _abs(self._box, path)
        self._box.files.write(p, _bytes(data))
        return WriteInfo(name=posixpath.basename(p), type=FileType.FILE, path=p)

    def write_files(self, files: Sequence[Any], user: Optional[str] = None, request_timeout: Optional[float] = None,
                    gzip: bool = False, use_octet_stream: Optional[bool] = None,
                    metadata: Optional[Dict[str, str]] = None) -> List[WriteInfo]:
        out = []
        for f in files:
            p, d = (f["path"], f["data"]) if isinstance(f, Mapping) else (f.path, f.data)
            out.append(self.write(p, d, user=user, metadata=metadata))
        return out

    @_e2b
    def list(self, path: str, depth: Optional[int] = 1, user: Optional[str] = None,  # noqa: A003
             request_timeout: Optional[float] = None) -> List[EntryInfo]:
        _refuse_user("Filesystem.list", user)
        if depth is not None and depth < 1:
            raise InvalidArgumentException("depth should be at least 1")
        out: List[EntryInfo] = []
        self._walk(_abs(self._box, path), depth or 1, out)
        return out

    def _walk(self, path: str, depth: int, out: List[EntryInfo]) -> None:
        for e in self._box.files.list(path):
            info = _entry(e)
            if not info.path:
                info.path = posixpath.join(path, info.name)
            out.append(info)
            if depth > 1 and info.type is FileType.DIR:
                self._walk(info.path, depth - 1, out)

    @_e2b
    def exists(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> bool:
        _refuse_user("Filesystem.exists", user)
        return stat_entry(self._box, _abs(self._box, path)) is not None

    @_e2b
    def get_info(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> EntryInfo:
        _refuse_user("Filesystem.get_info", user)
        e = stat_entry(self._box, _abs(self._box, path))
        if e is None:
            raise FileNotFoundException(f"path {_abs(self._box, path)} does not exist")
        info = _entry(e)
        info.path = info.path or _abs(self._box, path)
        return info

    @_e2b
    def remove(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> None:
        _refuse_user("Filesystem.remove", user)
        try:
            self._box.files.delete(_abs(self._box, path), recursive=True)
        except Kosa8NotFoundError:
            pass  # E2B: removing what is not there is not an error

    @_e2b
    def rename(self, old_path: str, new_path: str, user: Optional[str] = None,
               request_timeout: Optional[float] = None) -> EntryInfo:
        """Runs ``mv`` in the sandbox."""
        _refuse_user("Filesystem.rename", user)
        r = self._box.exec(["mv", _abs(self._box, old_path), _abs(self._box, new_path)])
        if r.exit_code != 0:
            if "No such file" in r.stderr or "cannot stat" in r.stderr:
                raise FileNotFoundException(f"path {_abs(self._box, old_path)} does not exist")
            raise SandboxException(f"rename failed: {r.stderr.strip()}")
        return self.get_info(new_path)

    @_e2b
    def make_dir(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> bool:
        """False when it already existed, as in E2B."""
        _refuse_user("Filesystem.make_dir", user)
        if stat_entry(self._box, _abs(self._box, path)) is not None:
            return False
        self._box.files.mkdir(_abs(self._box, path))
        return True

    def watch_dir(self, path: str, *a: Any, **kw: Any) -> Any:
        raise Unsupported("e2b Filesystem.watch_dir", "poll files.list(path) for changes")


# --- the sandbox ---------------------------------------------------------------------------


def _create_body(
    template: Optional[str], timeout: Optional[int], metadata: Optional[Dict[str, str]], envs: Optional[Dict[str, str]],
    allow_internet_access: Optional[bool], mcp: Any, network: Any, iam: Any, lifecycle: Any, volume_mounts: Any,
    idempotency_key: Optional[str], cpu_count: Optional[int], opts: Mapping[str, Any],
) -> Dict[str, Any]:
    for name, v, alt in (
        ("mcp", mcp, "run the MCP server yourself with commands.run(..., background=True)"),
        ("iam", iam, "org secrets: Kosa8Cloud().sandboxes.create(secrets=[...])"),
        ("volume_mounts", volume_mounts, "keep data in /workspace, or snapshot the sandbox (create_snapshot)"),
        ("idempotency_key", idempotency_key, "look the sandbox up by name: Kosa8Cloud().sandboxes.list(name=...)"),
        ("cpu_count", cpu_count, "Kosa8Cloud().sandboxes.create(size='2x4') or cpus= with mem_mb="),
    ):
        if v:
            raise Unsupported(f"e2b Sandbox.create({name}=...)", alt)
    for k in ("domain", "api_url"):
        check_url(f"e2b Sandbox.create({k}=...)", opts.get(k))
    body: Dict[str, Any] = {}
    policy = _egress("Sandbox.create", network) if network else None
    sleep_on_timeout = False
    if lifecycle:
        on_timeout = lifecycle.get("on_timeout") if isinstance(lifecycle, Mapping) else None
        action = on_timeout.get("action") if isinstance(on_timeout, Mapping) else on_timeout
        if action not in (None, "kill", "pause") or set(lifecycle) - {"on_timeout", "auto_resume"}:
            raise Unsupported("e2b Sandbox.create(lifecycle=...)", "lifecycle={'on_timeout': 'pause'} or 'kill'")
        if lifecycle.get("auto_resume") is True:
            raise Unsupported("e2b Sandbox.create(lifecycle={'auto_resume': True})",
                              "a kosa8 sandbox wakes for the next command by itself; leave auto_resume out")
        sleep_on_timeout = action == "pause"
    if sleep_on_timeout:
        body["sleep_after_s"] = min(_E2B_DEFAULT_TIMEOUT if timeout is None else int(timeout), 604800)
    else:
        body["ttl_s"] = _ttl(timeout)
    if template and template.startswith("snap_"):
        body["snapshot"] = template
    elif template:
        # E2B's own default ("base") has Python in it; kosa8's base is Alpine
        # without it, so E2B's "base" starts kosa8's "python" template.
        body["template"] = "python" if template == "base" else template
    if envs:
        body["env"] = dict(envs)
    if metadata:
        body["labels"] = dict(metadata)
    if allow_internet_access is False:
        policy = {**(policy or {}), "internet": False}
        if policy.get("deny") and not policy.get("allow"):
            policy.pop("deny")
    if policy is not None:
        body["egress"] = policy
    return body


#: E2B's ``ALL_TRAFFIC``.
ALL_TRAFFIC = "0.0.0.0/0"

_NETWORK_REFUSED = {
    "rules": "kosa8 does not rewrite requests on the way out; send the headers from your code, or use org secrets",
    "egress_proxy": "set HTTPS_PROXY / ALL_PROXY in envs",
    "mask_request_host": "previews are served at their own host: sandbox.get_host(port)",
    "https_ports": "a preview speaks HTTPS to the caller and plain HTTP to the port; serve plain HTTP inside",
    "dns_servers": "leave it out; kosa8 resolves allowed names for the sandbox",
}


class _SelectorContext:
    """What E2B passes to an ``allow_out`` / ``deny_out`` callable."""

    all_traffic = ALL_TRAFFIC

    def __init__(self) -> None:
        self.rules: Mapping[str, Any] = {}


def _selector(v: Any) -> List[str]:
    if v is None:
        return []
    if callable(v):
        v = v(_SelectorContext())
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _egress(method: str, network: Any) -> Dict[str, Any]:
    """E2B's network options as a kosa8 egress policy.

    E2B lets everything out unless ``deny_out`` says otherwise, and
    ``allow_out`` wins over ``deny_out``. ``deny_out=[ALL_TRAFFIC]`` with an
    ``allow_out`` list is an allow-list (kosa8 ``internet: false`` with
    ``allow``); a ``deny_out`` of some hosts blocks just those."""
    n = network if isinstance(network, Mapping) else vars(network)
    for k, alt in _NETWORK_REFUSED.items():
        if n.get(k) not in (None, [], {}):
            raise Unsupported(f"e2b {method}(network={{'{k}': ...}})", alt)
    allow = _selector(n.get("allow_out"))
    deny = _selector(n.get("deny_out"))
    if ALL_TRAFFIC in allow:
        return {"internet": True, "deny": []}
    if ALL_TRAFFIC in deny:
        return {"internet": False, "allow": allow, "deny": [d for d in deny if d != ALL_TRAFFIC and d not in allow]}
    return {"internet": True, "deny": [d for d in deny if d not in allow]}


def _private_traffic(network: Any) -> bool:
    if not network:
        return False
    n = network if isinstance(network, Mapping) else vars(network)
    return n.get("allow_public_traffic") is False


def _base_url(opts: Mapping[str, Any]) -> Optional[str]:
    """A kosa8 address named the E2B way: ``api_url=``, ``domain=`` or
    ``E2B_DOMAIN`` (``kosa8.com`` is ``https://api.kosa8.com``, as E2B makes
    ``https://api.<domain>``). An ``E2B_DOMAIN`` that is not kosa8's is
    another provider's and is left alone."""
    if opts.get("api_url"):
        return str(opts["api_url"])
    domain = opts.get("domain") or os.environ.get("E2B_DOMAIN", "").strip()
    if not domain or not is_kosa8_url(domain):
        return None
    host = domain.split("//", 1)[-1].rstrip("/")
    if host.startswith(("localhost", "127.0.0.1")):
        return "http://" + host
    return "https://" + (host if host.startswith("api.") else "api." + host)


#: The E2B key variable; read when it holds a kosa8 key.
KEY_ENV = ("E2B_API_KEY",)


def _cloud(api_key: Optional[str] = None, **opts: Any) -> Kosa8Cloud:
    return get_cloud(api_key, key_env=KEY_ENV, base_url=_base_url(opts))


def _kill_id(cloud: Kosa8Cloud, sandbox_id: str) -> bool:
    try:
        cloud._request("DELETE", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id, safe='')}")
    except Kosa8NotFoundError:
        return False
    return True


class Sandbox:
    """E2B's ``Sandbox``. Make one with :meth:`create` or :meth:`connect`.

    ``sandbox.kosa8`` is the :class:`kosa8.CloudSandbox` underneath.
    """

    #: E2B's default image has Python, so the shim starts kosa8's ``python``
    #: template unless ``template=`` says otherwise.
    _default_template: Optional[str] = "python"
    #: Whether create() asks for a persistent sandbox: one long-lived
    #: machine, as on E2B, so installed packages, files outside /workspace
    #: and background processes last between commands (the code interpreter
    #: needs one too). ``KOSA8_SHIM_PERSISTENT=0`` turns it off; a plan with
    #: no persistent sandboxes left gets a plain one, with a warning.
    _persistent: bool = True
    #: The original SDK's module and class, for ``KOSA8_FALLBACK=e2b``.
    _fallback = ("e2b", "Sandbox")

    def __init__(self, *, _box: Optional[CloudSandbox] = None, **opts: Any) -> None:
        if _box is None:
            # E2B's constructor is deprecated in favour of create(); it still creates.
            _box = type(self).create(**opts).kosa8
        self.kosa8 = _box
        #: E2B's envd version; kosa8 answers the same calls without envd.
        self._envd_version = "kosa8"
        self._private_traffic = False
        self._hosts: Dict[int, str] = {}
        self._files = Filesystem(_box)
        self._commands = Commands(_box)

    def __repr__(self) -> str:
        return f"Sandbox(sandbox_id={self.sandbox_id!r})"

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.kill()

    @property
    def sandbox_id(self) -> str:
        return self.kosa8.id

    @property
    def files(self) -> Filesystem:
        return self._files

    @property
    def commands(self) -> Commands:
        return self._commands

    @property
    def pty(self) -> Any:
        raise Unsupported("e2b Sandbox.pty", "sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal")

    @property
    def git(self) -> Any:
        raise Unsupported("e2b Sandbox.git (deprecated by E2B)", "commands.run('git ...') in an image with git")

    # create / connect ----------------------------------------------------------------

    @classmethod
    def create(
        cls,
        template: Optional[str] = None,
        timeout: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        envs: Optional[Dict[str, str]] = None,
        secure: Optional[bool] = None,
        allow_internet_access: Optional[bool] = None,
        mcp: Any = None,
        network: Any = None,
        iam: Any = None,
        lifecycle: Any = None,
        volume_mounts: Any = None,
        logger: Any = None,
        idempotency_key: Optional[str] = None,
        *,
        cpu_count: Optional[int] = None,
        api_key: Optional[str] = None,
        **opts: Any,
    ) -> "Sandbox":
        """Start a sandbox (``template`` is a kosa8 template key; the default,
        and E2B's ``base``, is kosa8's ``python``, Debian with Python 3; a
        ``snap_...`` id starts from that snapshot). It is persistent, like an
        E2B sandbox. ``network`` maps to kosa8 egress (``allow_out`` /
        ``deny_out``, ``allow_public_traffic``). ``secure`` is ignored, as E2B
        now ignores it; ``logger`` is not used.

        With ``KOSA8_FALLBACK=e2b``, the ``e2b`` package installed and an E2B
        key in ``E2B_API_KEY``, a create kosa8 cannot do runs on E2B instead
        (and returns E2B's own ``Sandbox``)."""
        args = dict(template=template, timeout=timeout, metadata=metadata, envs=envs, secure=secure,
                    allow_internet_access=allow_internet_access, mcp=mcp, network=network, iam=iam, lifecycle=lifecycle,
                    volume_mounts=volume_mounts, logger=logger, idempotency_key=idempotency_key)
        try:
            return cls._create(args, cpu_count, api_key, opts)
        except Unsupported as e:
            def original() -> Any:
                mod, name = cls._fallback
                real = getattr(__import__(mod, fromlist=[name]), name)
                kw = {k: v for k, v in args.items() if v is not None}
                if cpu_count is not None:
                    kw["cpu_count"] = cpu_count
                return real.create(**kw, **{k: v for k, v in opts.items() if k not in ("domain", "api_url")})
            return fallback("e2b", e, lambda: foreign_key("E2B_API_KEY") and bool(__import__(cls._fallback[0])), original)

    @classmethod
    @_e2b
    def _create(cls, args: Dict[str, Any], cpu_count: Optional[int], api_key: Optional[str], opts: Dict[str, Any]) -> "Sandbox":
        a = args
        template = a["template"]
        body = _create_body(template if template is not None else cls._default_template, a["timeout"], a["metadata"],
                            a["envs"], a["allow_internet_access"], a["mcp"], a["network"], a["iam"], a["lifecycle"],
                            a["volume_mounts"], a["idempotency_key"], cpu_count, opts)
        cloud = _cloud(api_key, **opts)
        labels = body.pop("labels", None)
        box = create_sandbox(cloud, persistent=cls._persistent, labels=labels, **body)
        sb = cls(_box=box)
        sb._private_traffic = _private_traffic(a["network"])
        return sb

    def _connect_self(self, timeout: Optional[int] = None, **opts: Any) -> "Sandbox":
        return type(self)._connect_cls(self.sandbox_id, timeout, **opts)

    @classmethod
    @_e2b
    def _connect_cls(cls, sandbox_id: str, timeout: Optional[int] = None, logger: Any = None, *,
                     on_resume: str = "restore", api_key: Optional[str] = None, **opts: Any) -> "Sandbox":
        if on_resume != "restore":
            raise Unsupported(f"e2b Sandbox.connect(on_resume={on_resume!r})", "on_resume='restore' (memory and disk kept)")
        box = _cloud(api_key, **opts).sandboxes.get(sandbox_id)
        if box.state == "sleeping":
            box.wake()
        if timeout is not None:
            # E2B's connect only ever lengthens a running sandbox's life.
            end = box.info.get("delete_after")
            want = int(time.time() * 1000) + _ttl(timeout) * 1000
            if not isinstance(end, (int, float)) or want > end:
                box.update(ttl_s=_ttl(timeout))
        return cls(_box=box)

    #: ``Sandbox.connect(sandbox_id)`` or ``sandbox.connect()``; wakes a paused sandbox.
    connect = Hybrid(_connect_self, lambda cls, *a, **kw: cls._connect_cls(*a, **kw))

    # lifecycle -----------------------------------------------------------------------

    def _kill_self(self, **opts: Any) -> bool:
        return _e2b(_kill_id)(self.kosa8._cloud, self.sandbox_id)

    #: Delete it; False when there was no such sandbox.
    kill = Hybrid(_kill_self, lambda cls, sandbox_id, api_key=None, **o: _e2b(_kill_id)(_cloud(api_key), sandbox_id))

    def _set_timeout_self(self, timeout: int, **opts: Any) -> None:
        _e2b(self.kosa8.update)(ttl_s=_ttl(timeout))

    #: A new lifetime, counted from now (kosa8 keeps a sandbox 60 s at least).
    set_timeout = Hybrid(
        _set_timeout_self,
        lambda cls, sandbox_id, timeout, api_key=None, **o: _e2b(_cloud(api_key).sandboxes.get(sandbox_id).update)(ttl_s=_ttl(timeout)),
    )

    def _get_info_self(self, **opts: Any) -> SandboxInfo:
        return _info(_e2b(self.kosa8.refresh)().info)

    get_info = Hybrid(
        _get_info_self,
        lambda cls, sandbox_id, api_key=None, **o: _info(_e2b(_cloud(api_key).sandboxes.get)(sandbox_id).info),
    )

    @_e2b
    def is_running(self, request_timeout: Optional[float] = None) -> bool:
        try:
            return self.kosa8.refresh().state == "running"
        except Kosa8NotFoundError:
            return False

    def _pause_self(self, keep_memory: Optional[bool] = None, *, mode: Any = None, strict: bool = False, **opts: Any) -> bool:
        return _e2b(_pause)(self.kosa8, keep_memory, mode)

    #: Sleep it (kosa8 keeps memory, processes and disk); False when it was already asleep.
    pause = Hybrid(
        _pause_self,
        lambda cls, sandbox_id, keep_memory=None, *, mode=None, strict=False, api_key=None, **o:
            _e2b(_pause)(_cloud(api_key).sandboxes.get(sandbox_id), keep_memory, mode),
    )
    beta_pause = pause

    def _fork_self(self, timeout: Optional[int] = None, count: Optional[int] = None, **opts: Any) -> List["Sandbox"]:
        return type(self)._fork_box(self.kosa8, timeout, count)

    @classmethod
    @_e2b
    def _fork_box(cls, box: CloudSandbox, timeout: Optional[int], count: Optional[int]) -> List["Sandbox"]:
        copies = [cls(_box=b) for b in box.fork(n=1 if count is None else count)]
        if timeout is not None:
            for c in copies:
                c.kosa8.update(ttl_s=_ttl(timeout))
        return copies

    #: Live copies of the sandbox as it is now (memory included).
    fork = Hybrid(
        _fork_self,
        lambda cls, sandbox_id, timeout=None, count=None, logger=None, api_key=None, **o:
            cls._fork_box(_cloud(api_key).sandboxes.get(sandbox_id), timeout, count),
    )

    def _snapshot_self(self, name: Optional[str] = None, *, mode: Any = None, **opts: Any) -> SnapshotInfo:
        return _e2b(_snapshot)(self.kosa8, name, mode)

    #: A durable snapshot; start a sandbox from it with ``Sandbox.create(template=info.snapshot_id)``.
    create_snapshot = Hybrid(
        _snapshot_self,
        lambda cls, sandbox_id, name=None, *, mode=None, api_key=None, **o:
            _e2b(_snapshot)(_cloud(api_key).sandboxes.get(sandbox_id), name, mode),
    )

    def _list_snapshots_self(self, limit: Optional[int] = None, next_token: Optional[str] = None,
                             name: Optional[str] = None, **opts: Any) -> SnapshotPaginator:
        return _list_snapshots(self.kosa8._cloud, self.sandbox_id, name)

    list_snapshots = Hybrid(
        _list_snapshots_self,
        lambda cls, sandbox_id=None, limit=None, next_token=None, name=None, api_key=None, **o:
            _list_snapshots(_cloud(api_key), sandbox_id, name),
    )

    @staticmethod
    @_e2b
    def delete_snapshot(snapshot_id: str, api_key: Optional[str] = None, **opts: Any) -> bool:
        try:
            _cloud(api_key).snapshots.delete(snapshot_id)
        except Kosa8NotFoundError:
            return False
        return True

    @staticmethod
    @_e2b
    def list(query: Any = None, limit: Optional[int] = None, next_token: Optional[str] = None,  # noqa: A003
             api_key: Optional[str] = None, **opts: Any) -> SandboxPaginator:
        """Running and paused sandboxes. ``query`` may filter by ``metadata`` (labels)
        and ``state`` (``running``, ``paused``)."""
        q = query if isinstance(query, Mapping) else (vars(query) if query is not None else {})
        labels = q.get("metadata") or None
        states = q.get("state") or ["running", "paused"]
        cloud = _cloud(api_key)

        def fetch() -> List[SandboxInfo]:
            boxes = cloud.sandboxes.list(labels=labels)
            infos = [_info(b.info) for b in boxes if b.state in ("running", "sleeping", "creating")]
            return [i for i in infos if i.state in states or (i.state == "creating" and "running" in states)]

        return SandboxPaginator(_e2b(fetch))

    # the rest ---------------------------------------------------------------------------

    @_e2b
    def download_url(self, path: str, user: Optional[str] = None, use_signature_expiration: Optional[int] = None) -> str:
        """A presigned link to download ``path`` (valid up to 300 s)."""
        _refuse_user("Sandbox.download_url", user)
        ttl = None if use_signature_expiration is None else min(max(int(use_signature_expiration), 10), 300)
        return self.kosa8.files.link(_abs(self.kosa8, path), method="GET", ttl_s=ttl)["url"]

    def upload_url(self, path: Optional[str] = None, *a: Any, **kw: Any) -> str:
        raise Unsupported("e2b Sandbox.upload_url", "sandbox.kosa8.files.link(path, method='PUT')['url'] takes a PUT of the raw bytes (not E2B's multipart POST)")

    @_e2b
    def get_host(self, port: int) -> str:
        """The host serving ``port`` over HTTPS (``https://{host}``), as in E2B.

        It is a public kosa8 preview of the port, opened on first use: like
        an E2B sandbox URL, anyone with the host reaches the port. A sandbox
        created with ``network={"allow_public_traffic": False}`` has no
        public host; use ``sandbox.kosa8.previews.create(port)["url"]``, a
        signed private link, there."""
        if self._private_traffic:
            raise Unsupported("e2b Sandbox.get_host with allow_public_traffic=False",
                              "sandbox.kosa8.previews.create(port)['url'] is a signed private link (the token is in the URL, "
                              "not E2B's e2b-traffic-access-token header)")
        port = int(port)
        if port not in self._hosts:
            self._hosts[port] = self.kosa8.previews.create(port, public=True)["host"]
        return self._hosts[port]

    def get_metrics(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("e2b Sandbox.get_metrics", "sandbox.kosa8.metrics() (kosa8's own fields)")

    def _update_network_self(self, network: Any, **opts: Any) -> None:
        _e2b(_set_network)(self.kosa8, network)

    #: Replace the sandbox's egress rules (kosa8 egress), as E2B's update does:
    #: what ``network`` leaves out is cleared. ``allow_internet_access``
    #: may be given too.
    update_network = Hybrid(
        _update_network_self,
        lambda cls, sandbox_id, network, api_key=None, **o: _e2b(_set_network)(_cloud(api_key, **o).sandboxes.get(sandbox_id), network),
    )

    def get_mcp_token(self) -> Any:
        raise Unsupported("e2b Sandbox.get_mcp_token", "run your MCP server with commands.run(..., background=True)")

    def get_mcp_url(self) -> Any:
        raise Unsupported("e2b Sandbox.get_mcp_url", "expose its port with sandbox.kosa8.previews.create(port)")

    beta_get_mcp_token = get_mcp_token

    # code interpreter (e2b_code_interpreter's Sandbox adds these) -----------------------

    @_e2b
    def run_code(
        self,
        code: str,
        language: Optional[str] = None,
        context: Optional["Context"] = None,
        on_stdout: Optional[Callable[["OutputMessage"], None]] = None,
        on_stderr: Optional[Callable[["OutputMessage"], None]] = None,
        on_result: Optional[Callable[["Result"], None]] = None,
        on_error: Optional[Callable[["ExecutionError"], None]] = None,
        envs: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        request_timeout: Optional[float] = None,
    ) -> "Execution":
        """Run a Python cell; globals persist between calls (per context).

        Callbacks are called once each, when the cell ends. ``timeout`` is at
        most 80 s on kosa8. ``is_main_result`` marks the last result that has
        text, which is the cell's last expression unless the cell ended in
        ``display()`` calls.
        """
        if language is not None and context is not None:
            raise InvalidArgumentException("You can provide context or language, but not both at the same time.")
        lang = context.language if context is not None else language
        if lang not in (None, "python", "py", "python3"):
            raise Unsupported(f"e2b Sandbox.run_code(language={lang!r})",
                              "Python only; run other languages with commands.run (node -e, ...)")
        if envs:
            raise Unsupported("e2b Sandbox.run_code(envs=...)", "set env on the sandbox (Sandbox.create(envs=...)) or os.environ in the cell")
        r = self.kosa8.run_code(code, context=context.id if context is not None else None, timeout_s=timeout) or {}
        ex = _execution(r)
        now = time.time()
        if on_stdout:
            for line in ex.logs.stdout:
                on_stdout(OutputMessage(line, now, False))
        if on_stderr:
            for line in ex.logs.stderr:
                on_stderr(OutputMessage(line, now, True))
        if on_result:
            for res in ex.results:
                on_result(res)
        if on_error and ex.error is not None:
            on_error(ex.error)
        return ex

    @_e2b
    def create_code_context(self, cwd: Optional[str] = None, language: Optional[str] = None,
                            request_timeout: Optional[float] = None) -> "Context":
        if cwd is not None:
            raise Unsupported("e2b Sandbox.create_code_context(cwd=...)", "os.chdir(...) in the context's first cell")
        if language not in (None, "python", "py", "python3"):
            raise Unsupported(f"e2b Sandbox.create_code_context(language={language!r})", "Python contexts only")
        c = self.kosa8.code_contexts.create()
        return Context(id=c["name"], language="python", cwd="/workspace")

    @_e2b
    def list_code_contexts(self) -> List["Context"]:
        return [Context(id=c["name"], language="python", cwd="/workspace") for c in self.kosa8.code_contexts.list()]

    @_e2b
    def remove_code_context(self, context: Union["Context", str]) -> None:
        self.kosa8.code_contexts.delete(context.id if isinstance(context, Context) else context)

    @_e2b
    def restart_code_context(self, context: Union["Context", str]) -> None:
        self.kosa8.code_contexts.reset(context.id if isinstance(context, Context) else context)


def _set_network(box: CloudSandbox, network: Any) -> None:
    n = dict(network if isinstance(network, Mapping) else vars(network or {}))
    internet = n.pop("allow_internet_access", None)
    if "allow_public_traffic" in n:
        raise Unsupported("e2b Sandbox.update_network(allow_public_traffic=...)",
                          "set it on create; open private previews with sandbox.kosa8.previews.create(port)")
    p = _egress("Sandbox.update_network", n)
    if internet is False:
        p["internet"] = False
    if p["internet"]:
        box.set_egress(internet=True, allow=[], deny=p.get("deny", []))
    else:
        box.set_egress(internet=False, allow=p.get("allow", []), deny=p.get("deny", []))


def _pause(box: CloudSandbox, keep_memory: Optional[bool], mode: Any) -> bool:
    if keep_memory is False or mode not in (None, "full", "memory"):
        raise Unsupported("e2b Sandbox.pause(filesystem only)", "pause() keeps memory and disk; delete the sandbox if you need neither")
    if box.refresh().state == "sleeping":
        return False
    box.sleep()
    return True


def _snapshot(box: CloudSandbox, name: Optional[str], mode: Any) -> SnapshotInfo:
    if mode not in (None, "full", "memory"):
        raise Unsupported("e2b Sandbox.create_snapshot(mode=...)", "kosa8 snapshots always keep memory and disk")
    s = box.snapshot(name)
    return SnapshotInfo(snapshot_id=s["id"], name=s.get("name"), sandbox_id=s.get("source_sandbox_id") or box.id, state=s.get("state"))


def _list_snapshots(cloud: Kosa8Cloud, sandbox_id: Optional[str], name: Optional[str]) -> SnapshotPaginator:
    def fetch() -> List[SnapshotInfo]:
        snaps = cloud.snapshots.list(sandbox_id=sandbox_id)
        out = [SnapshotInfo(snapshot_id=s["id"], name=s.get("name"), sandbox_id=s.get("source_sandbox_id"),
                            state=s.get("state")) for s in snaps]
        return [s for s in out if name is None or s.name == name]

    return SnapshotPaginator(_e2b(fetch))


# --- code interpreter results -------------------------------------------------------------


@dataclass
class OutputMessage:
    line: str
    timestamp: float
    error: bool = False

    def __str__(self) -> str:
        return self.line


@dataclass
class ExecutionError:
    name: str
    value: str
    traceback: str


@dataclass
class Logs:
    stdout: List[str] = field(default_factory=list)
    stderr: List[str] = field(default_factory=list)


@dataclass
class Result:
    text: Optional[str] = None
    html: Optional[str] = None
    markdown: Optional[str] = None
    svg: Optional[str] = None
    png: Optional[str] = None
    jpeg: Optional[str] = None
    pdf: Optional[str] = None
    latex: Optional[str] = None
    json: Optional[Any] = None
    javascript: Optional[str] = None
    data: Optional[Any] = None
    chart: Optional[Any] = None
    is_main_result: bool = False
    extra: Optional[Dict[str, Any]] = None

    def formats(self) -> List[str]:
        return [k for k in ("text", "html", "markdown", "svg", "png", "jpeg", "pdf", "latex", "json", "javascript")
                if getattr(self, k) is not None]

    def __str__(self) -> str:
        return self.text or ""


@dataclass
class Execution:
    results: List[Result] = field(default_factory=list)
    logs: Logs = field(default_factory=Logs)
    error: Optional[ExecutionError] = None
    execution_count: Optional[int] = None

    @property
    def text(self) -> Optional[str]:
        for r in self.results:
            if r.is_main_result:
                return r.text
        return None


@dataclass
class Context:
    id: str
    language: str
    cwd: str


_KIND = {"text": "text", "html": "html", "json": "json", "image/png": "png", "image/svg+xml": "svg"}


def _lines(s: str) -> List[str]:
    return [ln + "\n" for ln in s.splitlines()] if s else []


def _execution(r: Mapping[str, Any]) -> Execution:
    results: List[Result] = []
    cur: Optional[Result] = None
    for item in r.get("results") or []:
        kind = _KIND.get(item.get("type"))
        if kind is None:
            continue
        # One object comes as its richest format and then its text.
        if cur is None or getattr(cur, kind) is not None:
            cur = Result()
            results.append(cur)
        setattr(cur, kind, item.get("data"))
        if kind == "text":
            cur = None
    with_text = [x for x in results if x.text is not None]
    if with_text:
        with_text[-1].is_main_result = True
    err = r.get("error")
    return Execution(
        results=results,
        logs=Logs(stdout=_lines(r.get("stdout") or ""), stderr=_lines(r.get("stderr") or "")),
        error=ExecutionError(err.get("name", ""), err.get("value", ""), err.get("traceback", "")) if isinstance(err, Mapping) else None,
        execution_count=r.get("execution_count"),
    )
