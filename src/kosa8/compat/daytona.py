# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Daytona's SDK on kosa8 Cloud: change ``from daytona import Daytona`` (and
its params classes) to ``from kosa8.compat.daytona import ...``.

Mirrors the synchronous API of the ``daytona`` Python SDK **2.1.0**, from its
public reference https://www.daytona.io/docs/en/python-sdk/ (``sync/daytona``,
``sync/sandbox``, ``sync/process``, ``sync/file-system``, ``sync/snapshot``,
``common/errors``; checked 2026-11-21). ``AsyncDaytona`` is not mirrored.

The key is a kosa8 Cloud key: ``DaytonaConfig(api_key="k8c_live_...")`` or
``KOSA8_API_KEY``. ``DAYTONA_API_KEY`` is not read, and an ``api_url`` that is
not kosa8's is refused, so the kosa8 key never goes to another provider.

What maps, and how:

==================================  =========================================
Daytona                             kosa8 Cloud
==================================  =========================================
``Daytona().create(params)``        ``cloud.sandboxes.create``: ``snapshot``
                                    is a kosa8 template key (or a ``snap_...``
                                    snapshot), ``image`` an OCI image,
                                    ``resources`` cpus / mem_mb / disk_gb,
                                    ``env_vars`` env, ``labels`` labels,
                                    ``auto_stop_interval`` /
                                    ``auto_pause_interval`` (minutes) sleep
                                    after idle, ``ttl_minutes`` ttl_s,
                                    ``network_block_all`` no internet,
                                    ``target`` region
``get`` / ``list`` / ``delete``     get (by id or name) / list / delete
``start`` / ``stop`` / ``pause``    wake / sleep / sleep
``sandbox.process.exec``            exec (``sh -c``; ``cwd`` relative to
                                    /workspace); streamed when ``timeout``
                                    is over 90 s
``process.*session*``               each command a streamed exec: logs
                                    arrive as it prints
                                    (``get_session_command_logs_async``),
                                    ``run_async`` runs it on a thread; cd and
                                    export do not carry between commands
``sandbox.process.code_run``        ``python3 -c`` (or ``node -e``) as an exec
``sandbox.fs.*``                    the sandbox's files API
``sandbox.fork``                    ``fork(n=1)``
``sandbox.create_snapshot(name)``   a durable kosa8 snapshot
``daytona.snapshot.list/get/        ``cloud.snapshots``
delete``
==================================  =========================================

Daytona snapshots are images you build; kosa8 snapshots are saved sandboxes
(memory and disk). ``daytona.snapshot.create`` (an image build) raises
:class:`kosa8.compat.Unsupported`, as does anything else kosa8 cannot do.
Only ``/workspace`` outlives a command. A buffered exec (``process.exec``
with no ``timeout`` over 90 s) runs at most 90 s; session commands and a
``timeout`` over 90 s are streamed and are not held to it.
"""

from __future__ import annotations

import math
import os
import posixpath
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from ..cloud import EXEC_LIMIT_S, CloudSandbox, Kosa8Cloud
from ..errors import (
    Kosa8AuthError,
    Kosa8BadRequestError,
    Kosa8ConflictError,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8PolicyError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
)
from . import StreamPump, Unsupported, abs_path, check_url, get_cloud, given, in_dir, stat_entry, translating

__all__ = [
    "Daytona", "DaytonaConfig", "CreateSandboxFromSnapshotParams", "CreateSandboxFromImageParams", "Resources",
    "ListSandboxesQuery", "CodeRunParams", "FileUpload", "Sandbox", "Process", "FileSystem", "SnapshotService",
    "ExecuteResponse", "ExecutionArtifacts", "FileInfo", "Snapshot", "PaginatedSnapshots", "PortPreviewUrl",
    "CodeLanguage", "SessionExecuteRequest", "SessionExecuteResponse", "SessionCommandLogsResponse", "Session",
    "Command", "DaytonaError", "DaytonaNotFoundError", "DaytonaAuthenticationError", "DaytonaForbiddenError",
    "DaytonaRateLimitError", "DaytonaConflictError", "DaytonaBadRequestError", "DaytonaTimeoutError",
    "DaytonaConnectionError", "DaytonaFileNotFoundError", "Unsupported",
]

#: The Daytona SDK version this module mirrors.
MIRRORS = {"daytona": "2.1.0"}


# --- errors, as daytona.common.errors names them ------------------------------------------


class DaytonaError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, headers: Optional[Mapping[str, Any]] = None,
                 error_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.error_code = error_code


class DaytonaNotFoundError(DaytonaError):
    pass


class DaytonaAuthenticationError(DaytonaError):
    pass


class DaytonaForbiddenError(DaytonaError):
    pass


class DaytonaRateLimitError(DaytonaError):
    pass


class DaytonaConflictError(DaytonaError):
    pass


class DaytonaBadRequestError(DaytonaError):
    pass


class DaytonaTimeoutError(DaytonaError):
    pass


class DaytonaConnectionError(DaytonaError):
    pass


class DaytonaFileNotFoundError(DaytonaNotFoundError):
    pass


_TABLE = (
    (Kosa8ExecTimeoutError, DaytonaTimeoutError),
    (Kosa8TimeoutError, DaytonaTimeoutError),
    (Kosa8NotFoundError, DaytonaNotFoundError),
    (Kosa8AuthError, DaytonaAuthenticationError),
    (Kosa8PolicyError, DaytonaForbiddenError),
    (Kosa8RateLimitError, DaytonaRateLimitError),
    (Kosa8ConflictError, DaytonaConflictError),
    (Kosa8BadRequestError, DaytonaBadRequestError),
    (Kosa8ConnectionError, DaytonaConnectionError),
)


def _convert(e: Kosa8Error) -> BaseException:
    cls = next((o for k, o in _TABLE if isinstance(e, k)), DaytonaError)
    return cls(str(e), status_code=e.status, error_code=e.code)


_dt = translating(_convert)


# --- config and params ----------------------------------------------------------------------


class CodeLanguage:
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"


@dataclass
class DaytonaConfig:
    api_key: Optional[str] = None
    jwt_token: Optional[str] = None
    organization_id: Optional[str] = None
    api_url: Optional[str] = None
    server_url: Optional[str] = None
    target: Optional[str] = None
    connection_pool_maxsize: Optional[int] = None
    otel_enabled: Optional[bool] = None
    use_deprecated_polling: Optional[bool] = None
    _experimental: Optional[Dict[str, Any]] = None


@dataclass
class Resources:
    cpu: Optional[int] = None
    memory: Optional[int] = None  # GiB
    disk: Optional[int] = None  # GiB
    gpu: Optional[int] = None
    gpu_type: Any = None


@dataclass
class _CreateBase:
    name: Optional[str] = None
    language: Optional[str] = None
    os_user: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None
    labels: Optional[Dict[str, str]] = None
    public: Optional[bool] = None
    timeout: Optional[float] = None
    auto_stop_interval: Optional[int] = None
    auto_pause_interval: Optional[int] = None
    auto_archive_interval: Optional[int] = None
    auto_delete_interval: Optional[int] = None
    ttl_minutes: Optional[int] = None
    queue_timeout: Optional[int] = None
    volumes: Optional[List[Any]] = None
    secrets: Optional[Dict[str, str]] = None
    network_block_all: Optional[bool] = None
    kvm: Optional[bool] = None
    network_allow_list: Optional[str] = None
    domain_allow_list: Optional[str] = None
    outbound_proxy_url: Optional[str] = None
    otel_endpoint_override: Optional[str] = None
    ephemeral: Optional[bool] = None
    spot: Optional[bool] = None
    linked_sandbox: Optional[str] = None
    target: Optional[str] = None


@dataclass
class CreateSandboxFromSnapshotParams(_CreateBase):
    snapshot: Optional[str] = None


@dataclass
class CreateSandboxFromImageParams(_CreateBase):
    image: Any = None
    resources: Optional[Resources] = None


@dataclass
class ListSandboxesQuery:
    limit: Optional[int] = None
    id: Optional[str] = None
    name: Optional[str] = None
    labels: Optional[Dict[str, str]] = None
    states: Optional[List[str]] = None


@dataclass
class CodeRunParams:
    argv: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None


@dataclass
class FileUpload:
    source: Union[bytes, str]
    destination: str


# --- results -----------------------------------------------------------------------------------


@dataclass
class ExecutionArtifacts:
    stdout: str = ""
    charts: Optional[List[Any]] = None


@dataclass
class ExecuteResponse:
    exit_code: Optional[int]
    result: str
    artifacts: Optional[ExecutionArtifacts] = None


@dataclass
class SessionExecuteRequest:
    command: str
    run_async: Optional[bool] = None
    #: Deprecated in Daytona in favour of ``run_async``; either works.
    var_async: Optional[bool] = None
    suppress_input_echo: Optional[bool] = False


@dataclass
class SessionExecuteResponse:
    cmd_id: str
    output: str = ""
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None


@dataclass
class SessionCommandLogsResponse:
    output: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


@dataclass
class Command:
    id: str  # noqa: A003 (Daytona's name)
    command: str
    exit_code: Optional[int] = None


@dataclass
class Session:
    session_id: str
    commands: Optional[List[Command]] = None


@dataclass
class FileInfo:
    name: str
    is_dir: bool
    size: int = 0
    mode: str = ""
    mod_time: str = ""
    permissions: str = ""
    owner: str = ""
    group: str = ""


@dataclass
class Snapshot:
    id: str
    name: Optional[str]
    state: Optional[str] = None
    image_name: Optional[str] = None
    cpu: Optional[int] = None
    mem: Optional[float] = None
    disk: Optional[int] = None
    created_at: Optional[str] = None
    size: Optional[int] = None


@dataclass
class PaginatedSnapshots:
    items: List[Snapshot] = field(default_factory=list)
    total: int = 0
    page: int = 1
    total_pages: int = 1


@dataclass
class PortPreviewUrl:
    url: str
    token: Optional[str] = None


def _file_info(e: Mapping[str, Any]) -> FileInfo:
    mode = e.get("mode")
    return FileInfo(
        name=e.get("name") or posixpath.basename(e.get("path", "")),
        is_dir=e.get("type") == "dir",
        size=int(e.get("size") or 0),
        mode=str(mode) if mode is not None else "",
        mod_time=str(e.get("mod_time") or ""),
        permissions=str(mode) if mode is not None else "",
    )


def _snap(s: Mapping[str, Any]) -> Snapshot:
    mem = s.get("mem_mb")
    return Snapshot(id=s["id"], name=s.get("name"), state=s.get("state"), image_name=s.get("image") or s.get("template"),
                    cpu=s.get("cpus"), mem=(mem / 1024) if isinstance(mem, (int, float)) else None,
                    disk=s.get("disk_gb"), created_at=s.get("created_at"), size=s.get("bytes"))


_STATES = {"running": "started", "sleeping": "stopped", "creating": "starting", "error": "error", "deleted": "destroyed"}


# --- process ---------------------------------------------------------------------------------------


def _session(name: str) -> Callable[..., Any]:
    def nope(self: Any, *a: Any, **kw: Any) -> Any:
        raise Unsupported(f"daytona Process.{name}", "process.exec per command; for long work, sandbox.kosa8.exec_background(...)")

    nope.__name__ = name
    return nope


class _SessionCommand:
    """One command run in a session: a streamed exec, read on a thread."""

    def __init__(self, cmd_id: str, command: str, pump: StreamPump) -> None:
        self.id = cmd_id
        self.command = command
        self.pump = pump

    @property
    def exit_code(self) -> Optional[int]:
        r = self.pump.result
        return r.exit_code if self.pump.done and r is not None else None

    def logs(self) -> SessionCommandLogsResponse:
        return SessionCommandLogsResponse(output=self.pump.text(), stdout=self.pump.text("stdout"),
                                          stderr=self.pump.text("stderr"))

    def info(self) -> Command:
        return Command(id=self.id, command=self.command, exit_code=self.exit_code)


class Process:
    """``sandbox.process``."""

    def __init__(self, sandbox: "Sandbox") -> None:
        self._sb = sandbox
        self._sessions: Dict[str, Dict[str, _SessionCommand]] = {}

    @_dt
    def exec(self, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
             timeout: Optional[int] = None) -> ExecuteResponse:
        """Run a shell command (``sh -c``). ``result`` is stdout then stderr,
        and a non-zero exit is returned, not raised, as in Daytona.
        ``timeout`` is applied with ``timeout(1)`` in the sandbox; over 90 s,
        the command runs as a streamed exec, which is not held to kosa8's
        90 s for a buffered one. With no ``timeout`` it is buffered (90 s at
        most); for longer work without a bound, use a session command."""
        argv = ["sh", "-c", command]
        if timeout:
            argv = ["timeout", str(int(timeout)), *argv]
        return self._run(in_dir(argv, cwd), env, stream=bool(timeout and timeout > EXEC_LIMIT_S))

    def _run(self, argv: List[str], env: Optional[Mapping[str, str]], stream: bool = False) -> ExecuteResponse:
        r = self._sb.kosa8.exec(argv, env=env, stream=stream or None)
        out = r.stdout + r.stderr
        return ExecuteResponse(exit_code=r.exit_code, result=out, artifacts=ExecutionArtifacts(stdout=out))

    @_dt
    def code_run(self, code: str, params: Optional[CodeRunParams] = None, timeout: Optional[int] = None) -> ExecuteResponse:
        """Run ``code`` with the sandbox's language (``python3 -c``, or ``node -e``
        for javascript). Each run is a fresh process, as in Daytona. Charts are
        not collected (``artifacts.charts`` is None); for charts and state that
        persists, use ``sandbox.kosa8.run_code``."""
        lang = self._sb._language or CodeLanguage.PYTHON
        if lang == CodeLanguage.PYTHON:
            argv = ["python3", "-c", code]
        elif lang == CodeLanguage.JAVASCRIPT:
            argv = ["node", "-e", code]
        else:
            raise Unsupported(f"daytona Process.code_run (language {lang!r})",
                              "process.exec('npx tsx file.ts') in an image with Node, or a Python or JavaScript sandbox")
        if params is not None and params.argv:
            argv += (["--"] if lang == CodeLanguage.JAVASCRIPT else []) + list(params.argv)
        if timeout:
            argv = ["timeout", str(int(timeout)), *argv]
        return self._run(argv, params.env if params is not None else None, stream=bool(timeout and timeout > EXEC_LIMIT_S))

    # sessions: each command is a streamed exec, so its logs arrive as it
    # prints and it is not held to 90 s. kosa8 runs every command in a fresh
    # container: a session groups commands and their logs, but cd and export
    # do not carry from one command to the next (files in /workspace do).
    # Sessions live in this Process object, not on the sandbox.

    def _session_cmds(self, session_id: str) -> Dict[str, _SessionCommand]:
        cmds = self._sessions.get(session_id)
        if cmds is None:
            raise DaytonaNotFoundError(f"session {session_id!r} not found", status_code=404)
        return cmds

    def _session_cmd(self, session_id: str, command_id: str) -> _SessionCommand:
        c = self._session_cmds(session_id).get(command_id)
        if c is None:
            raise DaytonaNotFoundError(f"command {command_id!r} not found in session {session_id!r}", status_code=404)
        return c

    def create_session(self, session_id: str, request_timeout: Optional[float] = None) -> None:
        if session_id in self._sessions:
            raise DaytonaConflictError(f"session {session_id!r} already exists", status_code=409)
        self._sessions[session_id] = {}

    def get_session(self, session_id: str, request_timeout: Optional[float] = None) -> Session:
        return Session(session_id=session_id, commands=[c.info() for c in self._session_cmds(session_id).values()])

    def list_sessions(self, request_timeout: Optional[float] = None) -> List[Session]:
        return [self.get_session(sid) for sid in self._sessions]

    def delete_session(self, session_id: str, request_timeout: Optional[float] = None) -> None:
        """Forget the session and stop reading its commands' output."""
        for c in self._session_cmds(session_id).values():
            c.pump.close()
        del self._sessions[session_id]

    @_dt
    def execute_session_command(self, session_id: str, req: SessionExecuteRequest,
                                timeout: Optional[int] = None) -> SessionExecuteResponse:
        """Run ``req.command`` (``sh -c``) in the session. With ``run_async``
        it returns at once with ``cmd_id``; follow it with
        :meth:`get_session_command_logs_async` or poll :meth:`get_session_command`.
        Otherwise it waits and returns the output and exit code."""
        cmds = self._session_cmds(session_id)
        argv = ["sh", "-c", req.command]
        if timeout:
            argv = ["timeout", str(int(timeout)), *argv]
        cmd_id = f"cmd_{secrets.token_hex(8)}"
        pump = StreamPump(self._sb.kosa8.exec_stream(argv))
        cmds[cmd_id] = _SessionCommand(cmd_id, req.command, pump)
        if req.run_async or req.var_async:
            return SessionExecuteResponse(cmd_id=cmd_id)
        r = pump.wait()
        return SessionExecuteResponse(cmd_id=cmd_id, output=pump.text(), stdout=r.stdout, stderr=r.stderr,
                                      exit_code=r.exit_code)

    def get_session_command(self, session_id: str, command_id: str, request_timeout: Optional[float] = None) -> Command:
        """The command; ``exit_code`` is None while it runs."""
        return self._session_cmd(session_id, command_id).info()

    def get_session_command_logs(self, session_id: str, command_id: str,
                                 request_timeout: Optional[float] = None) -> SessionCommandLogsResponse:
        """Its output so far."""
        return self._session_cmd(session_id, command_id).logs()

    async def get_session_command_logs_async(self, session_id: str, command_id: str,
                                             on_stdout: Callable[[str], Any], on_stderr: Callable[[str], Any]) -> None:
        """Call ``on_stdout`` / ``on_stderr`` with the command's output as it
        prints (what it printed before this call first), until it ends.
        A callback may be a coroutine function."""
        import asyncio
        import inspect

        c = self._session_cmd(session_id, command_id)
        i = 0
        while True:
            batch = c.pump.chunks[i:]
            i += len(batch)
            for name, text in batch:
                got = (on_stdout if name == "stdout" else on_stderr)(text)
                if inspect.isawaitable(got):
                    await got
            if c.pump.done and i >= len(c.pump.chunks):
                return
            await asyncio.sleep(0.05)

    get_entrypoint_session = _session("get_entrypoint_session")
    get_entrypoint_logs = _session("get_entrypoint_logs")
    send_session_command_input = _session("send_session_command_input")

    def create_pty_session(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona Process.create_pty_session", "sandbox.kosa8.terminal() gives a wss:// URL for a terminal")

    connect_pty_session = create_pty_session
    list_pty_sessions = create_pty_session
    get_pty_session_info = create_pty_session
    kill_pty_session = create_pty_session
    resize_pty_session = create_pty_session


# --- file system --------------------------------------------------------------------------------------


class FileSystem:
    """``sandbox.fs``. Relative paths are under /workspace, the folder kosa8 keeps."""

    def __init__(self, sandbox: "Sandbox") -> None:
        self._sb = sandbox

    @property
    def _box(self) -> CloudSandbox:
        return self._sb.kosa8

    @_dt
    def upload_file(self, file: Union[bytes, str], remote_path: str, timeout: int = 30 * 60) -> None:
        """``file`` is the bytes, or a local file path to read."""
        if isinstance(file, str):
            with open(file, "rb") as f:
                data = f.read()
        else:
            data = bytes(file)
        self._box.files.write(abs_path(remote_path), data)

    def upload_files(self, files: List[FileUpload], timeout: int = 30 * 60) -> None:
        for f in files:
            self.upload_file(f.source, f.destination, timeout)

    @_dt
    def download_file(self, remote_path: str, local_path: Optional[Union[str, int]] = None,
                      timeout: int = 30 * 60) -> Optional[bytes]:
        """The bytes, or with ``local_path`` saved there (returns None)."""
        if isinstance(local_path, int):  # download_file(path, timeout) positional
            local_path, timeout = None, local_path
        try:
            data = self._box.files.read(abs_path(remote_path))
        except Kosa8NotFoundError as e:
            raise DaytonaFileNotFoundError(str(e), status_code=404) from e
        if local_path is None:
            return data
        d = os.path.dirname(local_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return None

    @_dt
    def list_files(self, path: str, depth: Optional[int] = None, request_timeout: Optional[float] = None) -> List[FileInfo]:
        if depth not in (None, 1):
            raise Unsupported("daytona FileSystem.list_files(depth=...)", "list each folder, or process.exec('find PATH -maxdepth N')")
        return [_file_info(e) for e in self._box.files.list(abs_path(path))]

    @_dt
    def get_file_info(self, path: str, request_timeout: Optional[float] = None) -> FileInfo:
        e = stat_entry(self._box, path)
        if e is None:
            raise DaytonaFileNotFoundError(f"file not found: {abs_path(path)}", status_code=404)
        return _file_info(e)

    @_dt
    def create_folder(self, path: str, mode: str = "755", request_timeout: Optional[float] = None) -> None:
        p = abs_path(path)
        self._box.files.mkdir(p)
        if mode and mode != "755":
            self._chmod(p, mode)

    @_dt
    def set_file_permissions(self, path: str, mode: Optional[str] = None, owner: Optional[str] = None,
                             group: Optional[str] = None, request_timeout: Optional[float] = None) -> None:
        """``chmod`` / ``chown`` in the sandbox."""
        p = abs_path(path)
        if mode:
            self._chmod(p, mode)
        if owner or group:
            who = (owner or "") + (f":{group}" if group else "")
            self._check(self._box.exec(["chown", who, p]), "chown")

    def _chmod(self, p: str, mode: str) -> None:
        self._check(self._box.exec(["chmod", mode, p]), "chmod")

    @staticmethod
    def _check(r: Any, what: str) -> None:
        if r.exit_code != 0:
            raise DaytonaError(f"{what} failed: {r.stderr.strip()}", status_code=None)

    @_dt
    def delete_file(self, path: str, recursive: bool = False, request_timeout: Optional[float] = None) -> None:
        self._box.files.delete(abs_path(path), recursive=recursive)

    @_dt
    def move_files(self, source: str, destination: str, request_timeout: Optional[float] = None) -> None:
        """``mv`` in the sandbox."""
        self._check(self._box.exec(["mv", abs_path(source), abs_path(destination)]), "move")

    def find_files(self, path: str, pattern: str, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.find_files", "process.exec(\"grep -rn PATTERN PATH\")")

    def search_files(self, path: str, pattern: str, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.search_files", "process.exec(\"find PATH -name 'GLOB'\")")

    def replace_in_files(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.replace_in_files", "process.exec(\"sed -i 's/old/new/g' FILES\")")

    def download_file_stream(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.download_file_stream", "download_file, or sandbox.kosa8.files.link(path) for a URL to stream")

    def upload_file_stream(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.upload_file_stream", "upload_file, or sandbox.kosa8.files.link(path, method='PUT')")

    def download_files(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("daytona FileSystem.download_files", "download_file for each file")


# --- the sandbox -------------------------------------------------------------------------------------------


class Sandbox:
    """A Daytona ``Sandbox``. ``sandbox.kosa8`` is the :class:`kosa8.CloudSandbox` underneath."""

    def __init__(self, box: CloudSandbox, language: Optional[str] = None) -> None:
        self.kosa8 = box
        self._language = language
        self.process = Process(self)
        self.fs = FileSystem(self)

    def __repr__(self) -> str:
        return f"Sandbox(id={self.id!r}, state={self.state!r})"

    # Daytona's fields, read from the kosa8 sandbox.
    @property
    def id(self) -> str:
        return self.kosa8.id

    @property
    def name(self) -> str:
        return self.kosa8.info.get("name") or self.kosa8.id

    @property
    def state(self) -> str:
        s = self.kosa8.state
        return _STATES.get(s, s)

    @property
    def labels(self) -> Dict[str, str]:
        return dict(self.kosa8.info.get("labels") or {})

    @property
    def cpu(self) -> Optional[int]:
        return self.kosa8.info.get("cpus")

    @property
    def memory(self) -> Optional[float]:
        m = self.kosa8.info.get("mem_mb")
        return m / 1024 if isinstance(m, (int, float)) else None

    @property
    def disk(self) -> Optional[int]:
        return self.kosa8.info.get("disk_gb")

    @property
    def target(self) -> Optional[str]:
        return self.kosa8.info.get("region")

    @property
    def created_at(self) -> Any:
        return self.kosa8.info.get("created_at")

    @property
    def git(self) -> Any:
        raise Unsupported("daytona Sandbox.git", "process.exec('git ...') in an image with git")

    @property
    def computer_use(self) -> Any:
        raise Unsupported("daytona Sandbox.computer_use", "a desktop image with its own VNC, exposed via sandbox.kosa8.previews.create(port)")

    @property
    def code_interpreter(self) -> Any:
        raise Unsupported("daytona Sandbox.code_interpreter", "sandbox.kosa8.run_code(code, context=...) (stateful Python cells)")

    @_dt
    def refresh_data(self) -> None:
        self.kosa8.refresh()

    def get_user_home_dir(self) -> str:
        return "/workspace"

    def get_work_dir(self) -> str:
        return "/workspace"

    @_dt
    def start(self, timeout: Optional[float] = 60) -> None:
        """Wake a stopped (sleeping) sandbox."""
        if self.kosa8.refresh().state == "sleeping":
            self.kosa8.wake()

    @_dt
    def stop(self, timeout: Optional[float] = 60, force: bool = False) -> None:
        """Sleep it: kosa8 keeps memory and disk, and it costs only storage."""
        if self.kosa8.refresh().state != "sleeping":
            self.kosa8.sleep()

    pause = stop

    def archive(self, request_timeout: Optional[float] = None) -> None:
        """A sleeping kosa8 sandbox is already kept as a snapshot in storage."""
        self.stop()

    @_dt
    def delete(self, timeout: Optional[float] = 60, wait: bool = False) -> None:
        self.kosa8.delete()

    def wait_for_sandbox_start(self, timeout: Optional[float] = 60) -> None:
        _dt(self.kosa8.wait_until_running)(timeout=timeout or 300)

    @_dt
    def set_labels(self, labels: Dict[str, str], request_timeout: Optional[float] = None) -> Dict[str, str]:
        return self.kosa8.set_labels(labels)

    @_dt
    def set_autostop_interval(self, interval: int, request_timeout: Optional[float] = None) -> None:
        """Minutes idle before it sleeps (0: never)."""
        if interval < 0:
            raise DaytonaError("auto_stop_interval must be a non-negative integer")
        self.kosa8.update(sleep_after_s=int(interval) * 60)

    set_auto_pause_interval = set_autostop_interval

    @_dt
    def set_ttl(self, ttl_minutes: int, request_timeout: Optional[float] = None) -> None:
        """Minutes until it is deleted, from now (0: no limit). kosa8 keeps a sandbox 1 minute at least."""
        if ttl_minutes == 0:
            self.kosa8.update(clear_lifetime=True)
        else:
            self.kosa8.update(ttl_s=max(int(ttl_minutes) * 60, 60))

    def set_auto_archive_interval(self, interval: int, request_timeout: Optional[float] = None) -> None:
        """Nothing to do: a sleeping kosa8 sandbox is already stored as a snapshot."""

    def set_auto_delete_interval(self, interval: int, request_timeout: Optional[float] = None) -> None:
        raise Unsupported("daytona Sandbox.set_auto_delete_interval", "set_ttl(minutes): kosa8 deletes on a lifetime, not after a stop")

    @_dt
    def get_preview_link(self, port: int, request_timeout: Optional[float] = None) -> PortPreviewUrl:
        """A private kosa8 preview of ``port``; ``url`` is a signed link (no separate token)."""
        p = self.kosa8.previews.create(port)
        return PortPreviewUrl(url=p["url"], token=None)

    @_dt
    def fork(self, name: Optional[str] = None, timeout: Optional[float] = 60) -> "Sandbox":
        """A live copy (memory included)."""
        if name is not None:
            raise Unsupported("daytona Sandbox.fork(name=...)", "fork() then copy.kosa8.set_labels({...}) to tell it apart")
        return Sandbox(self.kosa8.fork(n=1)[0], self._language)

    @_dt
    def create_snapshot(self, name: str, timeout: Optional[float] = 60) -> None:
        """A durable kosa8 snapshot (memory and disk). Start from it with
        ``CreateSandboxFromSnapshotParams(snapshot=<its snap_ id>)``."""
        self.kosa8.snapshot(name)

    def _refuse(name: str, alternative: str) -> Callable[..., Any]:  # type: ignore[misc]
        def nope(self: Any, *a: Any, **kw: Any) -> Any:
            raise Unsupported(f"daytona Sandbox.{name}", alternative)

        nope.__name__ = name
        return nope

    get_metrics = _refuse("get_metrics", "sandbox.kosa8.metrics()")
    get_metrics_latest = _refuse("get_metrics_latest", "sandbox.kosa8.metrics()")
    create_lsp_server = _refuse("create_lsp_server", "run the language server with process.exec in the background")
    set_public = _refuse("set_public", "sandbox.kosa8.previews.create(port, public=True)")
    create_signed_preview_url = _refuse("create_signed_preview_url", "get_preview_link(port) (already a signed link)")
    expire_signed_preview_url = _refuse("expire_signed_preview_url", "sandbox.kosa8.previews.rotate(port)")
    update_network_settings = _refuse("update_network_settings", "sandbox.kosa8.set_egress(internet=..., allow=[...])")
    update_secrets = _refuse("update_secrets", "org secrets: Kosa8Cloud().secrets.set(...) and create(secrets=[...])")
    update_env = _refuse("update_env", "pass env to process.exec, or create a new sandbox with env_vars")
    resize = _refuse("resize", "create a sized sandbox: CreateSandboxFromImageParams(resources=Resources(cpu=, memory=))")
    create_ssh_access = _refuse("create_ssh_access", "sandbox.kosa8.terminal() gives a wss:// terminal URL")
    revoke_ssh_access = _refuse("revoke_ssh_access", "sandbox.kosa8.terminal() gives a wss:// terminal URL")
    validate_ssh_access = _refuse("validate_ssh_access", "sandbox.kosa8.terminal() gives a wss:// terminal URL")
    recover = _refuse("recover", "delete it and create a new one (or start one from a snapshot)")
    download_url = _refuse("download_url", "sandbox.kosa8.files.link(path)['url']")
    upload_url = _refuse("upload_url", "sandbox.kosa8.files.link(path, method='PUT')['url']")
    del _refuse


# --- snapshots ----------------------------------------------------------------------------------------------


class SnapshotService:
    """``daytona.snapshot``: kosa8's durable snapshots (saved sandboxes)."""

    def __init__(self, cloud: Kosa8Cloud) -> None:
        self._cloud = cloud

    @_dt
    def list(self, page: Optional[int] = None, limit: Optional[int] = None,  # noqa: A003
             source_sandbox_id: Optional[str] = None) -> PaginatedSnapshots:
        items = [_snap(s) for s in self._cloud.snapshots.list(sandbox_id=source_sandbox_id)]
        total = len(items)
        if limit:
            pages = max(math.ceil(total / limit), 1)
            p = page or 1
            return PaginatedSnapshots(items=items[(p - 1) * limit:p * limit], total=total, page=p, total_pages=pages)
        return PaginatedSnapshots(items=items, total=total, page=1, total_pages=1)

    def _find(self, name: str) -> Mapping[str, Any]:
        if name.startswith("snap_"):
            return self._cloud.snapshots.get(name)
        for s in self._cloud.snapshots.list():
            if s.get("name") == name:
                return s
        raise Kosa8NotFoundError(f"no snapshot named {name}", status=404, code="not_found")

    @_dt
    def get(self, name: str) -> Snapshot:
        """By id (``snap_...``) or name."""
        return _snap(self._find(name))

    @_dt
    def delete(self, snapshot: Union[Snapshot, str]) -> None:
        sid = snapshot.id if isinstance(snapshot, Snapshot) else self._find(snapshot)["id"]
        self._cloud.snapshots.delete(sid)

    def create(self, params: Any, *a: Any, **kw: Any) -> Snapshot:
        raise Unsupported("daytona SnapshotService.create (an image build)",
                          "build and push the image yourself, then CreateSandboxFromImageParams(image='registry/name:tag'); "
                          "or snapshot a prepared sandbox with sandbox.create_snapshot(name)")

    def activate(self, snapshot: Any) -> Snapshot:
        raise Unsupported("daytona SnapshotService.activate", "kosa8 snapshots are ready once made; nothing to activate")


# --- the client ------------------------------------------------------------------------------------------------


_UNSUPPORTED_CREATE = (
    ("os_user", "commands run as the image's user; switch user inside the command"),
    ("public", "sandbox.kosa8.previews.create(port, public=True)"),
    ("volumes", "keep data in /workspace, or snapshot the sandbox"),
    ("secrets", "org secrets: Kosa8Cloud().sandboxes.create(secrets=[names])"),
    ("kvm", "a sized sandbox with docker=True via Kosa8Cloud().sandboxes.create"),
    ("network_allow_list", "sandbox.kosa8.set_egress(allow=[...]) after create"),
    ("domain_allow_list", "sandbox.kosa8.set_egress(allow=[...]) after create"),
    ("outbound_proxy_url", "set HTTPS_PROXY in env_vars"),
    ("otel_endpoint_override", "set OTEL_EXPORTER_OTLP_ENDPOINT in env_vars"),
    ("ephemeral", "ttl_minutes, or delete() when done"),
    ("auto_delete_interval", "ttl_minutes: kosa8 deletes on a lifetime, not after a stop"),
    ("spot", "kosa8 sandboxes are not preemptible; leave it out"),
    ("linked_sandbox", "fork() the sandbox you want to start from"),
    ("queue_timeout", "leave it out; a kosa8 create starts at once or fails"),
)


class Daytona:
    """Daytona's client, on kosa8 Cloud."""

    def __init__(self, config: Optional[DaytonaConfig] = None) -> None:
        config = config or DaytonaConfig()
        if config.jwt_token:
            raise Unsupported("daytona DaytonaConfig(jwt_token=...)", "a kosa8 Cloud API key: DaytonaConfig(api_key='k8c_live_...') or KOSA8_API_KEY")
        check_url("daytona DaytonaConfig(api_url=...)", config.api_url or config.server_url)
        self._cloud = get_cloud(config.api_key)
        self._target = config.target
        #: Durable snapshots.
        self.snapshot = SnapshotService(self._cloud)

    def __enter__(self) -> "Daytona":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def close(self) -> None:
        pass

    @_dt
    def create(
        self,
        params: Optional[Union[CreateSandboxFromSnapshotParams, CreateSandboxFromImageParams]] = None,
        *,
        timeout: float = 60,
        on_snapshot_create_logs: Optional[Callable[[str], None]] = None,
    ) -> Sandbox:
        """Start a sandbox and wait until it runs (``timeout`` seconds, 0: no
        limit). See the module docs for how each field maps."""
        p = params or CreateSandboxFromSnapshotParams()
        for name, alt in _UNSUPPORTED_CREATE:
            v = getattr(p, name, None)
            if given(v) and not (name == "auto_delete_interval" and v == -1):
                raise Unsupported(f"daytona create({type(p).__name__}({name}=...))", alt)
        body: Dict[str, Any] = {}
        snap = getattr(p, "snapshot", None)
        if snap:
            body["snapshot" if snap.startswith("snap_") else "template"] = snap
        image = getattr(p, "image", None)
        if image is not None:
            if not isinstance(image, str):
                raise Unsupported("daytona create(image=Image...) (a declarative image build)",
                                  "build and push the image, then image='registry/name:tag'")
            body["image"] = image
        res = getattr(p, "resources", None)
        if res is not None:
            if res.gpu or res.gpu_type:
                raise Unsupported("daytona Resources(gpu=...)", "kosa8 GPU hosts: contact kosa8 (not in the Cloud API yet)")
            if (res.cpu is None) != (res.memory is None):
                raise Unsupported("daytona Resources with only cpu or only memory",
                                  "give both cpu and memory, e.g. Resources(cpu=2, memory=4)")
            if res.cpu is not None:
                body["cpus"] = int(res.cpu)
                body["mem_mb"] = int(res.memory) * 1024
            if res.disk is not None:
                body["disk_gb"] = int(res.disk)
        lang = p.language
        if not snap and image is None and lang in (None, CodeLanguage.PYTHON):
            body["template"] = "python"
        idle = p.auto_pause_interval if p.auto_pause_interval is not None else p.auto_stop_interval
        if idle is not None:
            body["sleep_after_s"] = int(idle) * 60
        if p.ttl_minutes:
            body["ttl_s"] = max(int(p.ttl_minutes) * 60, 60)
        if p.env_vars:
            body["env"] = dict(p.env_vars)
        if p.name:
            body["name"] = p.name
        if p.network_block_all:
            body["egress"] = {"internet": False}
        region = p.target or self._target
        if region:
            body["region"] = region
        box = self._cloud.sandboxes.create(labels=p.labels or None, wait_timeout=timeout or 24 * 3600, **body)
        return Sandbox(box, lang)

    @_dt
    def get(self, sandbox_id_or_name: str, request_timeout: Optional[float] = None) -> Sandbox:
        if sandbox_id_or_name.startswith("sbx_"):
            return Sandbox(self._cloud.sandboxes.get(sandbox_id_or_name))
        found = self._cloud.sandboxes.list(name=sandbox_id_or_name)
        if not found:
            raise Kosa8NotFoundError(f"no sandbox with id or name {sandbox_id_or_name}", status=404, code="not_found")
        return Sandbox(found[0])

    @_dt
    def list(self, query: Optional[ListSandboxesQuery] = None,  # noqa: A003
             request_timeout: Optional[float] = None) -> Iterator[Sandbox]:
        q = query or ListSandboxesQuery()
        boxes = self._cloud.sandboxes.list(labels=q.labels or None, name=q.name)
        out = [Sandbox(b) for b in boxes]
        if q.id:
            out = [s for s in out if s.id.lower().startswith(q.id.lower())]
        if q.states:
            out = [s for s in out if s.state in q.states]
        return iter(out)

    @_dt
    def delete(self, sandbox: Sandbox, timeout: float = 60, wait: bool = False) -> None:
        sandbox.kosa8.delete()

    def start(self, sandbox: Sandbox, timeout: float = 60) -> None:
        sandbox.start(timeout)

    def stop(self, sandbox: Sandbox, timeout: float = 60) -> None:
        sandbox.stop(timeout)

    @property
    def volume(self) -> Any:
        raise Unsupported("daytona Daytona.volume", "keep data in /workspace, or snapshot the sandbox")

    @property
    def object_storage(self) -> Any:
        raise Unsupported("daytona Daytona.object_storage", "sandbox.kosa8.files.link(path) to move files to your own storage")
