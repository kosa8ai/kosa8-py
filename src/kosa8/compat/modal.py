# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Modal Sandboxes on kosa8 Cloud: change ``import modal`` to
``import kosa8.compat.modal as modal``.

Mirrors ``modal.Sandbox`` (and the ``App``, ``Image``, ``Secret``,
``ContainerProcess`` and ``StreamReader`` pieces sandbox code touches) of the
``modal`` Python SDK **1.6.4**, from its public reference
https://modal.com/docs/reference/modal.Sandbox,
https://modal.com/docs/reference/modal.container_process and
https://modal.com/docs/reference/modal.io_streams (checked 2026-11-21).
The ``.aio`` async variants, Functions and Volumes are not mirrored.

What maps, and how:

==============================  ===============================================
Modal                           kosa8 Cloud
==============================  ===============================================
``Sandbox.create(*args, ...)``  ``cloud.sandboxes.create``: ``image`` (from
                                ``Image.from_registry`` / ``debian_slim``) an
                                OCI image, ``timeout`` ttl_s, ``idle_timeout``
                                sleep after idle, ``env`` and
                                ``Secret.from_dict`` env, ``Secret.from_name``
                                org secrets, ``cpu`` + ``memory`` a size,
                                ``tags`` labels, ``block_network`` no
                                internet, ``region`` region. ``*args`` (the
                                entrypoint) run as a background command.
``sandbox.exec(*args)``         exec; the returned process has already finished
``wait`` / ``poll``             the entrypoint's background command (or, with
                                no entrypoint, the sandbox's state)
``stdout.read()`` / iteration   the entrypoint's output, when it ends
``terminate``                   delete
``snapshot_filesystem``         a durable snapshot, usable as ``image=``
``filesystem.*``                the sandbox's files API
``set_tags`` / ``get_tags``     labels
``Sandbox.from_id`` / ``list``  get / list
==============================  ===============================================

The default image is kosa8's ``python`` template (Modal's default is a slim
Debian with Python). Output arrives when a command ends, not line by line.
Only ``/workspace`` outlives a command, and an ``exec`` runs at most 90 s.
Image builds (``pip_install``, ``run_commands`` ...), GPUs, volumes, tunnels
and stdin raise :class:`kosa8.compat.Unsupported`.
"""

from __future__ import annotations

import enum
import os
import posixpath
import secrets as _secrets
import time
import types
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from ..cloud import MAX_TTL_S, MIN_TTL_S, CloudSandbox
from ..cloud_features import CloudBackgroundExec
from ..errors import (
    Kosa8AuthError,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8TimeoutError,
)
from . import Unsupported, abs_path, get_cloud, given, in_dir, stat_entry, translating

__all__ = [
    "App", "Image", "Secret", "Sandbox", "ContainerProcess", "StreamReader", "StreamWriter", "StreamType",
    "FileInfo", "exception", "Unsupported",
]

#: The Modal SDK version this module mirrors.
MIRRORS = {"modal": "1.6.4"}


# --- modal.exception --------------------------------------------------------------------------


class Error(Exception):
    pass


class NotFoundError(Error):
    pass


class AuthError(Error):
    pass


class ConnectionError(Error):  # noqa: A001 (Modal's name)
    pass


class TimeoutError(Error):  # noqa: A001
    pass


class SandboxTimeoutError(TimeoutError):
    pass


class ExecTimeoutError(TimeoutError):
    pass


class SandboxTerminatedError(Error):
    pass


class InvalidError(Error):
    pass


class SandboxFilesystemError(Error):
    pass


class SandboxFilesystemNotFoundError(SandboxFilesystemError):
    pass


#: ``modal.exception``: ``except modal.exception.NotFoundError`` works.
exception = types.SimpleNamespace(
    Error=Error, NotFoundError=NotFoundError, AuthError=AuthError, ConnectionError=ConnectionError,
    TimeoutError=TimeoutError, SandboxTimeoutError=SandboxTimeoutError, ExecTimeoutError=ExecTimeoutError,
    SandboxTerminatedError=SandboxTerminatedError, InvalidError=InvalidError,
    SandboxFilesystemError=SandboxFilesystemError, SandboxFilesystemNotFoundError=SandboxFilesystemNotFoundError,
)


def _convert(e: Kosa8Error) -> BaseException:
    if isinstance(e, Kosa8ExecTimeoutError):
        return ExecTimeoutError(str(e))
    if isinstance(e, Kosa8TimeoutError):
        return TimeoutError(str(e))
    if isinstance(e, Kosa8NotFoundError):
        return NotFoundError(str(e))
    if isinstance(e, Kosa8AuthError):
        return AuthError(str(e))
    if isinstance(e, Kosa8ConnectionError):
        return ConnectionError(str(e))
    return Error(str(e))


_md = translating(_convert)


# --- App, Image, Secret ---------------------------------------------------------------------------


class App:
    """kosa8 has no apps: an App is a name, put on its sandboxes as the
    ``modal-app`` label (so ``Sandbox.list(app_id=...)`` finds them)."""

    def __init__(self, name: Optional[str] = None, **kw: Any) -> None:
        self.name = name
        self.app_id = name

    def __repr__(self) -> str:
        return f"App({self.name!r})"

    @staticmethod
    def lookup(name: str, *, create_if_missing: bool = False, environment_name: Optional[str] = None,
               client: Any = None) -> "App":
        return App(name)


class Image:
    """An image reference. kosa8 boots OCI images as they are; it does not build them."""

    def __init__(self, ref: Optional[str] = None, *, template: Optional[str] = None, snapshot: Optional[str] = None) -> None:
        self.ref = ref
        self.template = template
        self.snapshot = snapshot
        self.object_id = snapshot or ref or template

    def __repr__(self) -> str:
        return f"Image({self.object_id!r})"

    @staticmethod
    def from_registry(tag: str, secret: Any = None, *, setup_dockerfile_commands: Sequence[str] = (),
                      force_build: bool = False, add_python: Optional[str] = None, **kw: Any) -> "Image":
        if secret is not None:
            raise Unsupported("modal Image.from_registry(secret=...)", "a public image, or a kosa8 template built from your private one")
        if setup_dockerfile_commands or add_python:
            raise Unsupported("modal Image.from_registry(setup_dockerfile_commands / add_python)",
                              "build and push an image with those steps, then Image.from_registry('registry/name:tag')")
        return Image(tag)

    @staticmethod
    def debian_slim(python_version: Optional[str] = None, force_build: bool = False) -> "Image":
        """``python:<version>-slim`` (Debian slim with Python), 3.12 by default."""
        return Image(f"python:{python_version or '3.12'}-slim")

    @staticmethod
    def from_id(image_id: str, client: Any = None) -> "Image":
        """A kosa8 snapshot id (``snap_...``, from :meth:`Sandbox.snapshot_filesystem`) or a template key."""
        if image_id.startswith("snap_"):
            return Image(snapshot=image_id)
        return Image(template=image_id)

    def _build_step(name: str) -> Any:  # type: ignore[misc]
        def nope(self: "Image", *a: Any, **kw: Any) -> "Image":
            raise Unsupported(
                f"modal Image.{name}",
                "kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); "
                "or set a sandbox up once and reuse it with snapshot_filesystem()",
            )

        nope.__name__ = name
        return nope

    pip_install = _build_step("pip_install")
    uv_pip_install = _build_step("uv_pip_install")
    apt_install = _build_step("apt_install")
    run_commands = _build_step("run_commands")
    dockerfile_commands = _build_step("dockerfile_commands")
    add_local_file = _build_step("add_local_file")
    add_local_dir = _build_step("add_local_dir")
    add_local_python_source = _build_step("add_local_python_source")
    pip_install_from_requirements = _build_step("pip_install_from_requirements")
    workdir = _build_step("workdir")
    env = _build_step("env")
    del _build_step

    @staticmethod
    def from_dockerfile(*a: Any, **kw: Any) -> "Image":
        raise Unsupported("modal Image.from_dockerfile", "build and push the image yourself, then Image.from_registry('registry/name:tag')")


class Secret:
    """``Secret.from_dict`` becomes env; ``Secret.from_name`` a kosa8 org secret of that name."""

    def __init__(self, env: Optional[Dict[str, str]] = None, name: Optional[str] = None) -> None:
        self.env = dict(env or {})
        self.name = name

    @staticmethod
    def from_dict(env_dict: Mapping[str, Optional[str]] = {}) -> "Secret":  # noqa: B006 (Modal's default)
        return Secret({k: v for k, v in env_dict.items() if v is not None})

    @staticmethod
    def from_name(name: str, *, environment_name: Optional[str] = None, required_keys: Sequence[str] = (),
                  client: Any = None) -> "Secret":
        return Secret(name=name)

    @staticmethod
    def from_local_environ(env_keys: Sequence[str]) -> "Secret":
        return Secret({k: os.environ[k] for k in env_keys if k in os.environ})

    @staticmethod
    def from_dotenv(*a: Any, **kw: Any) -> "Secret":
        raise Unsupported("modal Secret.from_dotenv", "Secret.from_dict(dotenv_values('.env'))")


def _secret_env(method: str, secrets: Optional[Sequence[Secret]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for s in secrets or ():
        if not isinstance(s, Secret):
            raise InvalidError(f"{method}: secrets must be Secret objects from kosa8.compat.modal")
        env.update(s.env)
    return env


# --- streams and processes ---------------------------------------------------------------------------


class StreamType(enum.Enum):
    PIPE = "pipe"
    DEVNULL = "devnull"
    STDOUT = "stdout"


class StreamReader:
    """A finished stream: ``read()`` returns all of it, iterating gives its lines."""

    def __init__(self, fetch: Any, fd: int, text: bool = True) -> None:
        self._fetch = fetch
        self._fd = fd
        self._text = text
        self._data: Optional[str] = None

    @property
    def file_descriptor(self) -> int:
        return self._fd

    def _all(self) -> str:
        if self._data is None:
            self._data = self._fetch()
        return self._data

    def read(self) -> Union[str, bytes]:
        d = self._all()
        return d if self._text else d.encode()

    def __iter__(self) -> Iterator[Union[str, bytes]]:
        for line in self._all().splitlines(keepends=True):
            yield line if self._text else line.encode()


class StreamWriter:
    def write(self, data: Any) -> None:
        raise Unsupported("modal StreamWriter.write (stdin)", "kosa8 commands take no stdin; write the input to a file in /workspace and read it")

    def write_eof(self) -> None:
        raise Unsupported("modal StreamWriter.write_eof (stdin)", "kosa8 commands take no stdin")

    def drain(self) -> None:
        raise Unsupported("modal StreamWriter.drain (stdin)", "kosa8 commands take no stdin")


class ContainerProcess:
    """What ``sandbox.exec`` returns. The command has run to its end by the time you have it."""

    def __init__(self, stdout: str, stderr: str, returncode: Optional[int], text: bool) -> None:
        self.stdout = StreamReader(lambda: stdout, 1, text)
        self.stderr = StreamReader(lambda: stderr, 2, text)
        self.stdin = StreamWriter()
        self._code = returncode

    @property
    def returncode(self) -> Optional[int]:
        return self._code

    def poll(self) -> Optional[int]:
        return self._code

    def wait(self) -> Optional[int]:
        return self._code


# --- filesystem ------------------------------------------------------------------------------------------


@dataclass
class FileInfo:
    name: str
    path: str
    type: str
    size: int = 0
    permissions: Any = None
    modified_time: Any = None


def _fi(e: Mapping[str, Any], parent: str = "") -> FileInfo:
    name = e.get("name") or posixpath.basename(e.get("path", ""))
    return FileInfo(name=name, path=e.get("path") or posixpath.join(parent, name),
                    type="directory" if e.get("type") == "dir" else "file", size=int(e.get("size") or 0),
                    permissions=e.get("mode"), modified_time=e.get("mod_time"))


def _fs(fn: Any) -> Any:
    """Modal's filesystem errors for a missing path."""

    @_md
    def run(*a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        except Kosa8NotFoundError as e:
            raise SandboxFilesystemNotFoundError(str(e)) from e

    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


class SandboxFilesystem:
    """``sandbox.filesystem``. Paths are absolute, as Modal requires; only /workspace outlives a command."""

    def __init__(self, box: CloudSandbox) -> None:
        self._box = box

    @_fs
    def read_bytes(self, remote_path: str) -> bytes:
        return self._box.files.read(abs_path(remote_path))

    def read_text(self, remote_path: str) -> str:
        return self.read_bytes(remote_path).decode("utf-8")

    @_fs
    def write_bytes(self, data: bytes, remote_path: str) -> None:
        self._box.files.write(abs_path(remote_path), bytes(data))

    def write_text(self, data: str, remote_path: str) -> None:
        self.write_bytes(data.encode("utf-8"), remote_path)

    def copy_from_local(self, local_path: Union[str, os.PathLike], remote_path: str) -> None:
        with open(local_path, "rb") as f:
            self.write_bytes(f.read(), remote_path)

    def copy_to_local(self, remote_path: str, local_path: Union[str, os.PathLike]) -> None:
        data = self.read_bytes(remote_path)
        d = os.path.dirname(os.fspath(local_path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)

    @_fs
    def list_files(self, remote_path: str) -> List[FileInfo]:
        p = abs_path(remote_path)
        return [_fi(e, p) for e in self._box.files.list(p)]

    @_fs
    def make_directory(self, remote_path: str, *, create_parents: bool = True) -> None:
        if not create_parents:
            raise Unsupported("modal filesystem.make_directory(create_parents=False)", "make_directory(path) (parents are always made)")
        self._box.files.mkdir(abs_path(remote_path))

    @_fs
    def remove(self, remote_path: str, *, recursive: bool = False) -> None:
        self._box.files.delete(abs_path(remote_path), recursive=recursive)

    @_fs
    def stat(self, remote_path: str) -> FileInfo:
        e = stat_entry(self._box, remote_path)
        if e is None:
            raise SandboxFilesystemNotFoundError(f"{abs_path(remote_path)} does not exist")
        return _fi(e, posixpath.dirname(abs_path(remote_path)))

    def watch(self, *a: Any, **kw: Any) -> Any:
        raise Unsupported("modal filesystem.watch", "poll filesystem.list_files(path) for changes")


# --- the sandbox ----------------------------------------------------------------------------------------------


_ENTRY_DIR = "/workspace/.kosa8-modal"
_GONE = ("deleted", "stopped", "error", "host_removed", "host_lost")


def _ttl(timeout: Optional[int]) -> int:
    t = 300 if timeout is None else int(timeout)
    return min(max(t, MIN_TTL_S), MAX_TTL_S)


_CREATE_REFUSED = (
    ("gpu", "kosa8 GPU hosts are not in the Cloud API yet"),
    ("cloud", "leave it out; kosa8 picks the host (region= picks the region)"),
    ("volumes", "keep data in /workspace, or snapshot_filesystem() and start from it"),
    ("network_file_systems", "keep data in /workspace"),
    ("pty", "sandbox.kosa8.terminal() gives a wss:// terminal URL"),
    ("encrypted_ports", "sandbox.kosa8.previews.create(port) gives an https URL"),
    ("h2_ports", "sandbox.kosa8.previews.create(port)"),
    ("unencrypted_ports", "sandbox.kosa8.previews.create(port)"),
    ("custom_domain", "sandbox.kosa8.previews.create(port)"),
    ("proxy", "set HTTPS_PROXY in env"),
    ("outbound_cidr_allowlist", "sandbox.kosa8.set_egress(allow=[...]) after create"),
    ("outbound_domain_allowlist", "sandbox.kosa8.set_egress(allow=[...]) after create"),
    ("cidr_allowlist", "sandbox.kosa8.set_egress(allow=[...]) after create"),
    ("inbound_cidr_allowlist", "private previews (signed links): sandbox.kosa8.previews.create(port)"),
    ("readiness_probe", "poll sandbox.exec(...) until your check passes"),
    ("include_oidc_identity_token", "org secrets via Secret.from_name"),
    ("runtime", "leave it out; kosa8 sandboxes are microVMs"),
    ("_experimental_outbound_policy", "sandbox.kosa8.set_egress(...)"),
    ("_experimental_enable_snapshot", "leave it out; snapshot_filesystem() always keeps memory and disk"),
    ("experimental_options", "leave it out"),
    ("pty_info", "sandbox.kosa8.terminal()"),
)


class Sandbox:
    """``modal.Sandbox``. ``sandbox.kosa8`` is the :class:`kosa8.CloudSandbox` underneath."""

    def __init__(self, box: CloudSandbox, *, entry: Optional[CloudBackgroundExec] = None, entry_err: Optional[str] = None,
                 workdir: Optional[str] = None) -> None:
        self.kosa8 = box
        self._entry = entry
        self._entry_err = entry_err
        self._workdir = workdir
        self._code: Optional[int] = None
        self.filesystem = SandboxFilesystem(box)
        self.stdout = StreamReader(self._entry_stdout, 1)
        self.stderr = StreamReader(self._entry_stderr, 2)
        self.stdin = StreamWriter()

    def __repr__(self) -> str:
        return f"Sandbox({self.object_id!r})"

    @property
    def object_id(self) -> str:
        return self.kosa8.id

    @staticmethod
    @_md
    def create(
        *args: str,
        app: Optional[App] = None,
        name: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        image: Optional[Image] = None,
        env: Optional[Dict[str, Optional[str]]] = None,
        secrets: Optional[Sequence[Secret]] = None,
        timeout: int = 300,
        idle_timeout: Optional[int] = None,
        workdir: Optional[str] = None,
        region: Optional[Union[str, Sequence[str]]] = None,
        cpu: Optional[Union[float, tuple]] = None,
        memory: Optional[Union[int, tuple]] = None,
        block_network: bool = False,
        verbose: bool = False,
        client: Any = None,
        environment_name: Optional[str] = None,
        api_key: Optional[str] = None,
        **refused: Any,
    ) -> "Sandbox":
        """Start a sandbox; with ``*args``, run them as its entrypoint in the
        background (stdout and stderr kept for ``sandbox.stdout`` /
        ``sandbox.stderr``). ``timeout`` (seconds) deletes it, 60 s at least;
        ``idle_timeout`` sleeps it when idle, which keeps it for later."""
        for k, alt in _CREATE_REFUSED:
            v = refused.pop(k, None)
            if given(v):
                raise Unsupported(f"modal Sandbox.create({k}=...)", alt)
        if refused:
            raise TypeError(f"Sandbox.create() got unexpected keyword arguments: {', '.join(sorted(refused))}")
        body: Dict[str, Any] = {"ttl_s": _ttl(timeout)}
        img = image
        if img is None:
            body["template"] = "python"
        elif not isinstance(img, Image):
            raise InvalidError("image must be a kosa8.compat.modal.Image")
        elif img.snapshot:
            body["snapshot"] = img.snapshot
        elif img.template:
            body["template"] = img.template
        else:
            body["image"] = img.ref
        if idle_timeout is not None:
            body["sleep_after_s"] = int(idle_timeout)
        envs = {k: v for k, v in (env or {}).items() if v is not None}
        envs.update(_secret_env("Sandbox.create", secrets))
        if envs:
            body["env"] = envs
        names = [s.name for s in secrets or () if s.name]
        if names:
            body["secrets"] = names
        if (cpu is None) != (memory is None):
            raise Unsupported("modal Sandbox.create with only cpu or only memory", "give both, e.g. cpu=2, memory=4096")
        if cpu is not None:
            c = cpu[1] if isinstance(cpu, tuple) else cpu
            m = memory[1] if isinstance(memory, tuple) else memory
            body["cpus"] = max(int(-(-float(c) // 1)), 1)
            body["mem_mb"] = int(m)
        if block_network:
            body["egress"] = {"internet": False}
        if region:
            if not isinstance(region, str):
                if len(region) != 1:
                    raise Unsupported("modal Sandbox.create(region=[several])", "one region, e.g. region='us'")
                region = region[0]
            body["region"] = region
        if name:
            body["name"] = name
        labels = dict(tags or {})
        if app is not None and app.name:
            labels["modal-app"] = app.name
        box = get_cloud(api_key).sandboxes.create(labels=labels or None, **body)
        entry = entry_err = None
        if args:
            entry_err = f"{_ENTRY_DIR}/{_secrets.token_hex(6)}.stderr"
            argv = ["sh", "-c", 'mkdir -p "${0%/*}" && exec "$@" 2>"$0"', entry_err, *args]
            entry = box.exec_background(in_dir(argv, workdir))
        return Sandbox(box, entry=entry, entry_err=entry_err, workdir=workdir)

    @staticmethod
    @_md
    def from_id(sandbox_id: str, client: Any = None, api_key: Optional[str] = None) -> "Sandbox":
        """The sandbox by id. Its entrypoint (if any) is not followed from a new handle."""
        return Sandbox(get_cloud(api_key).sandboxes.get(sandbox_id))

    @staticmethod
    @_md
    def from_name(app_name: str, name: str, *, environment_name: Optional[str] = None, client: Any = None,
                  api_key: Optional[str] = None) -> "Sandbox":
        found = get_cloud(api_key).sandboxes.list(name=name, labels={"modal-app": app_name})
        if not found:
            raise NotFoundError(f"no sandbox named {name} in app {app_name}")
        return Sandbox(found[0])

    @staticmethod
    @_md
    def list(*, app_id: Optional[str] = None, tags: Optional[Dict[str, str]] = None, client: Any = None,  # noqa: A003
             api_key: Optional[str] = None) -> Iterator["Sandbox"]:
        labels: Dict[str, Any] = dict(tags or {})
        if app_id:
            labels["modal-app"] = app_id
        boxes = get_cloud(api_key).sandboxes.list(labels=labels or None)
        return iter([Sandbox(b) for b in boxes if b.state in ("running", "sleeping", "creating")])

    # running commands -----------------------------------------------------------------------

    @_md
    def exec(
        self,
        *args: str,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
        env: Optional[Dict[str, Optional[str]]] = None,
        secrets: Optional[Sequence[Secret]] = None,
        text: bool = True,
        bufsize: int = -1,
        pty: bool = False,
        _pty_info: Any = None,
        pty_info: Any = None,
    ) -> ContainerProcess:
        """Run a command to its end (90 s at most on kosa8) and return it.
        ``timeout`` shorter than that is applied with ``timeout(1)``."""
        if not args:
            raise InvalidError("exec needs a command")
        if pty or pty_info or _pty_info:
            raise Unsupported("modal Sandbox.exec(pty=True)", "sandbox.kosa8.terminal() gives a wss:// terminal URL")
        if any(s.name for s in secrets or ()):
            raise Unsupported("modal Sandbox.exec(secrets=[Secret.from_name(...)])",
                              "name org secrets when creating the sandbox: Sandbox.create(secrets=[...])")
        envs = {k: v for k, v in (env or {}).items() if v is not None}
        envs.update(_secret_env("Sandbox.exec", secrets))
        argv = list(args)
        if timeout:
            argv = ["timeout", str(int(timeout)), *argv]
        r = self.kosa8.exec(in_dir(argv, workdir or self._workdir), env=envs or None)
        out, err = r.stdout, r.stderr
        # StreamType.STDOUT prints to this process's stdout instead of keeping it.
        if stdout is StreamType.STDOUT:
            print(out, end="")
        if stderr is StreamType.STDOUT:
            print(err, end="")
        if stdout is not StreamType.PIPE:
            out = ""
        if stderr is not StreamType.PIPE:
            err = ""
        if timeout and r.exit_code in (124, 143):
            raise ExecTimeoutError(f"exec timed out after {timeout} s")
        return ContainerProcess(out, err, r.exit_code, text)

    # the entrypoint ---------------------------------------------------------------------------

    def _entry_done(self, block: bool, timeout: float = 24 * 3600) -> Optional[Dict[str, Any]]:
        if self._entry is None:
            return None
        s = self._entry.wait(timeout=timeout) if block else self._entry.status()
        if s.get("state") == "exited":
            self._code = s.get("exit")
        return s

    @_md
    def _entry_stdout(self) -> str:
        if self._entry is None:
            raise Unsupported("modal Sandbox.stdout without an entrypoint here",
                              "create it with Sandbox.create(*args) on this handle, or read output from sandbox.exec(...)")
        return (self._entry_done(True) or {}).get("output") or ""

    @_md
    def _entry_stderr(self) -> str:
        if self._entry is None:
            raise Unsupported("modal Sandbox.stderr without an entrypoint here",
                              "create it with Sandbox.create(*args) on this handle, or read output from sandbox.exec(...)")
        self._entry_done(True)
        r = self.kosa8.exec(["cat", self._entry_err or ""])
        return r.stdout if r.exit_code == 0 else ""

    @_md
    def wait(self, raise_on_termination: bool = True) -> None:
        """Until the entrypoint exits; with no entrypoint, until the sandbox ends (its ``timeout``)."""
        if self._entry is not None:
            self._entry_done(True)
            return
        while True:
            try:
                st = self.kosa8.refresh().state
            except Kosa8NotFoundError:
                st = "deleted"
            if st in _GONE:
                if raise_on_termination and st != "stopped":
                    raise SandboxTerminatedError(f"sandbox {self.object_id} ended ({st})")
                return
            time.sleep(2)

    @_md
    def poll(self) -> Optional[int]:
        """None while it runs, else the entrypoint's exit code."""
        if self._entry is not None:
            self._entry_done(False)
            return self._code
        try:
            st = self.kosa8.refresh().state
        except Kosa8NotFoundError:
            st = "deleted"
        return None if st not in _GONE else (self._code if self._code is not None else 0)

    @property
    def returncode(self) -> Optional[int]:
        return self._code

    @_md
    def terminate(self, *, wait: bool = False) -> Optional[int]:
        """Delete the sandbox. With ``wait``, the entrypoint's exit code if it had ended."""
        if self._entry is not None and self._code is None:
            self._entry_done(False)
        try:
            self.kosa8.delete()
        except Kosa8NotFoundError:
            pass
        return self._code if wait else None

    # the rest ----------------------------------------------------------------------------------

    @_md
    def snapshot_filesystem(self, timeout: int = 55, *, ttl: Optional[int] = 30 * 24 * 3600) -> Image:
        """A durable kosa8 snapshot (memory and disk, kept until deleted);
        ``Sandbox.create(image=it)`` starts a copy."""
        s = self.kosa8.snapshot()
        return Image(snapshot=s["id"])

    @_md
    def set_tags(self, tags: Dict[str, str], *, client: Any = None) -> None:
        keep = {k: v for k, v in (self.kosa8.info.get("labels") or {}).items() if k == "modal-app"}
        self.kosa8.set_labels({**keep, **tags})

    @_md
    def get_tags(self) -> Dict[str, str]:
        labels = dict(self.kosa8.refresh().info.get("labels") or {})
        labels.pop("modal-app", None)
        return labels

    def _refuse(name: str, alternative: str) -> Any:  # type: ignore[misc]
        def nope(self: Any, *a: Any, **kw: Any) -> Any:
            raise Unsupported(f"modal Sandbox.{name}", alternative)

        nope.__name__ = name
        return nope

    tunnels = _refuse("tunnels", "sandbox.kosa8.previews.create(port)['url'] for each port")
    create_connect_token = _refuse("create_connect_token", "sandbox.kosa8.previews.create(port) (a signed private link)")
    wait_until_ready = _refuse("wait_until_ready", "poll sandbox.exec(...) until your readiness check passes")
    snapshot_directory = _refuse("snapshot_directory", "snapshot_filesystem() (the whole sandbox)")
    mount_image = _refuse("mount_image", "start from a snapshot: Sandbox.create(image=snapshot)")
    unmount_image = _refuse("unmount_image", "start from a snapshot: Sandbox.create(image=snapshot)")
    reload_volumes = _refuse("reload_volumes", "keep data in /workspace")
    open = _refuse("open", "sandbox.filesystem.read_text / write_text / read_bytes / write_bytes")  # noqa: A003
    ls = _refuse("ls", "sandbox.filesystem.list_files(path)")
    mkdir = _refuse("mkdir", "sandbox.filesystem.make_directory(path)")
    rm = _refuse("rm", "sandbox.filesystem.remove(path, recursive=...)")
    watch = _refuse("watch", "poll sandbox.filesystem.list_files(path)")
    del _refuse

    @property
    def logs(self) -> Any:
        raise Unsupported("modal Sandbox.logs", "sandbox.kosa8.logs() (its last commands and their output)")
