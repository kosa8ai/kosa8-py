# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""A kosa8 runtime for Prime Intellect ``verifiers`` (v1).

Checked against verifiers' source (``verifiers/v1/runtimes``, 2026-11): a
custom runtime is a ``RuntimeBackend`` subclass named by ``import_path``,
with a ``config_class`` derived from ``CustomRuntimeConfig`` (declaring
``image`` and ``workdir`` because it runs task images)::

    [env.agent.runtime]
    type = "custom"
    import_path = "kosa8.rl.verifiers.Kosa8Backend"
    image = "ghcr.io/acme/task:1.0"     # or template = "python"
    lease_s = 3600
    allow = ["pypi.org"]                # optional network policy

The key comes from ``KOSA8_API_KEY``. Declared features:

* **task images** (``image = "..."``, any registry image).
* **user switching**: a command bound to another user runs through
  ``su -s /bin/sh -c ... -- <user>`` (the sandbox runs as root); files written for that
  user are ``chown``-ed to it.
* **port exposure**: ``expose(port)`` opens a public kosa8 preview and
  answers its ``https://`` URL; servers bind :data:`SERVICE_PORT` (8000) on
  0.0.0.0, as on Prime.
* **network policies**: the config is a ``NetworkPolicyConfig``; setup runs
  open, and ``prepare_execution`` applies the policy with the sandbox's egress
  API (``allow`` becomes an allowlist plus the framework's own routes,
  ``block`` a denylist), restored by ``prepare_execution(None)``.
* **stop confirmation**: ``verify_stopped`` checks the sandbox is gone.

Commands are streamed execs, so output arrives as it prints and nothing is
held to the 90 s of a buffered exec. The sandbox is persistent (one
long-lived container) unless ``persistent = false``, so background servers,
installed packages and files outside ``/workspace`` last for the rollout, and
``kill``/``terminate`` signal the real process. kosa8 commands take no
interactive stdin: bytes written to a process *before* its output is first
read are handed to it as its stdin (a file); a write after that raises
``SandboxError``, so stdin-driven protocols such as ACP do not run here.
Compose, memory and disk limits are not declared (the framework refuses them
before provisioning).

verifiers runtime suite (``tests/unit/test_runtimes.py --runtime``, verifiers
96f0707, 2026-11-23, live on api.kosa8.com): 22/22 pass, all 9 runtime cells
(run, exec, run_background, files, task_images, user_switching,
port_exposure, network_policies; compose refused as undeclared).

The classes are built on first use, so importing this module does not need
verifiers.
"""

from __future__ import annotations

import asyncio
import shlex
import threading
import uuid
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

_CLASSES: Dict[str, Any] = {}

#: Makes the :class:`~kosa8.AsyncCloud` each runtime uses (default: ``AsyncCloud()``,
#: key from ``KOSA8_API_KEY``). Set it for another key or address.
cloud_factory: Optional[Any] = None

#: The port a server hosted in a kosa8 runtime binds (on 0.0.0.0) to be exposed.
SERVICE_PORT = 8000

_PIDS = "/tmp/vf-pids"

# Runs as root: records its pid (so kill can reach it), enters the workdir,
# then execs the command with stdin from a file when one was written.
_LAUNCH = ('mkdir -p ' + _PIDS + ' && echo $$ > "' + _PIDS + '/$1"; d="$2"; f="$3"; shift 3; '
           'if [ -n "$d" ]; then cd "$d" || exit 1; fi; '
           'if [ -n "$f" ]; then exec "$@" < "$f"; fi; exec "$@"')

# Signals a recorded process and everything under it.
_SIGNAL = ('p=$(cat "' + _PIDS + '/$2" 2>/dev/null) || exit 0; '
           'tree() { for c in $(pgrep -P "$1" 2>/dev/null); do tree "$c"; done; echo "$1"; }; '
           'kill -s "$1" $(tree "$p") 2>/dev/null; exit 0')


def wrap_argv(argv: List[str], workdir: Optional[str]) -> List[str]:
    """``argv`` run in ``workdir`` (a kosa8 Cloud exec takes no cwd)."""
    if not workdir:
        return list(argv)
    return ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", workdir, *argv]


def as_user(argv: List[str], user: Optional[str], env: Dict[str, str]) -> List[str]:
    """``argv`` run as ``user`` with ``env`` (``su`` resets parts of the environment,
    so the variables are set again inside it)."""
    if user is None:
        return list(argv)
    inner = ["env", *[f"{k}={v}" for k, v in env.items()], *argv]
    return ["su", "-s", "/bin/sh", "-c", 'exec "$0" "$@"', "--", user, *inner]


def launch_argv(argv: List[str], pid_id: str, workdir: Optional[str], stdin_path: Optional[str]) -> List[str]:
    """The full command line of one process: pid file, workdir, stdin."""
    return ["sh", "-c", _LAUNCH, "sh", pid_id, workdir or "", stdin_path or "", *argv]


def egress_for(routes: Optional[List[str]], allow: List[str], block: List[str]) -> Dict[str, Any]:
    """The kosa8 egress policy for a verifiers network policy at ``prepare_execution``.

    ``routes`` None reopens the network (a trusted phase); otherwise the
    framework's own route hosts are always reachable."""
    if routes is None:
        return {"internet": True, "allow": [], "deny": []}
    if allow == ["*"]:
        return {"internet": True, "allow": [], "deny": [b for b in block if b != "*"]}
    hosts = [h for h in (urlsplit(r).hostname for r in routes) if h]
    entries = list(dict.fromkeys([*hosts, *allow]))
    return {"internet": False, "allow": entries, "deny": []}


def open_stream(box: Any, argv: List[str], env: Dict[str, str]) -> Tuple[Iterator[Tuple[str, str]], Any]:
    """Start a streamed exec on a blocking :class:`kosa8.CloudSandbox`: the
    ``(stream, text)`` iterator and the stream (``.exit_code`` once read). Tests
    replace this."""
    s = box.exec_stream(argv, env=env or None)
    return iter(s), s


def _classes() -> Dict[str, Any]:
    if _CLASSES:
        return _CLASSES
    try:
        from verifiers.v1.configs.runtime import CustomRuntimeConfig  # type: ignore
        from verifiers.v1.errors import SandboxError  # type: ignore
        from verifiers.v1.runtimes.backends.custom import CustomRuntimeInfo  # type: ignore
        from verifiers.v1.runtimes.base import RuntimeBackend, RuntimeProcess  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("kosa8.rl.verifiers needs verifiers v1: pip install verifiers") from e
    try:
        from verifiers.v1.configs.runtime import NetworkPolicyConfig  # type: ignore
    except ImportError:  # pragma: no cover (older verifiers)
        NetworkPolicyConfig = None

    from ..aio import AsyncCloud
    from ..cloud import _quiet_delete, CloudSandbox
    from ..errors import Kosa8NotFoundError

    bases = (CustomRuntimeConfig,) if NetworkPolicyConfig is None else (CustomRuntimeConfig, NetworkPolicyConfig)

    class Kosa8RuntimeConfig(*bases):  # type: ignore[misc, valid-type]
        image: Optional[str] = None
        workdir: Optional[str] = "/workspace"
        template: Optional[str] = None
        snapshot: Optional[str] = None
        cpus: Optional[int] = None
        mem_mb: Optional[int] = None
        region: Optional[str] = None
        lease_s: int = 3600
        persistent: bool = True
        labels: Dict[str, str] = {}

    class Kosa8Process(RuntimeProcess):  # type: ignore[misc, valid-type]
        """One streamed exec. It starts on the first read, wait, poll or
        signal, so bytes written before that become its stdin."""

        def __init__(self, backend: Any, runtime: Any, argv: List[str], env: Dict[str, str]) -> None:
            self._backend = backend
            self._user = getattr(runtime, "user", None)
            self._argv = list(argv)
            self._env = env
            self._id = uuid.uuid4().hex[:16]
            self._stdin = bytearray()
            self._start_lock = asyncio.Lock()
            self._started = False
            self._killed: Optional[int] = None
            self._queues: Dict[str, "asyncio.Queue[Optional[bytes]]"] = {"stdout": asyncio.Queue(), "stderr": asyncio.Queue()}
            self._exit: "asyncio.Future[int]" = asyncio.get_running_loop().create_future()
            self.stdout = self._read("stdout")
            self.stderr = self._read("stderr")

        async def _start(self) -> None:
            async with self._start_lock:
                if self._started:
                    return
                self._started = True
                if self._killed is not None:
                    self._finish(self._killed, None)
                    return
                be = self._backend
                stdin_path = None
                try:
                    if self._stdin:
                        stdin_path = f"/tmp/vf-stdin-{self._id}"
                        await be._box.files.write(stdin_path, bytes(self._stdin))
                    argv = launch_argv(as_user(self._argv, self._user, self._env), self._id,
                                       be.config.workdir, stdin_path)
                    it, stream = await asyncio.to_thread(open_stream, be._sync_box(), argv, self._env)
                except Exception as e:  # noqa: BLE001
                    self._finish(None, SandboxError(f"kosa8 exec failed: {e}"))
                    return
                loop = asyncio.get_running_loop()
                threading.Thread(target=self._pump, args=(loop, it, stream), name="kosa8-vf-exec", daemon=True).start()

        def _pump(self, loop: Any, it: Iterator[Tuple[str, str]], stream: Any) -> None:
            code: Optional[int] = None
            err: Optional[BaseException] = None
            try:
                for name, text in it:
                    data = text.encode() if isinstance(text, str) else bytes(text)
                    loop.call_soon_threadsafe(self._queues.get(name, self._queues["stdout"]).put_nowait, data)
                code = getattr(stream, "exit_code", None)
            except Exception as e:  # noqa: BLE001 (handed to wait)
                err = e
            finally:
                try:
                    loop.call_soon_threadsafe(self._finish, code, err)
                except RuntimeError:  # the loop is gone
                    pass

        def _finish(self, code: Optional[int], err: Optional[BaseException]) -> None:
            for q in self._queues.values():
                q.put_nowait(None)
            if self._exit.done():
                return
            if self._killed is not None and (err is not None or code is None):
                self._exit.set_result(self._killed)
            elif err is not None:
                self._exit.set_exception(err if isinstance(err, SandboxError) else SandboxError(f"kosa8 exec failed: {err}"))
            else:
                self._exit.set_result(1 if code is None else int(code))

        async def _read(self, which: str) -> AsyncIterator[bytes]:
            await self._start()
            q = self._queues[which]
            while True:
                chunk = await q.get()
                if chunk is None:
                    return
                yield chunk

        async def write(self, data: bytes) -> None:
            if self._started:
                raise SandboxError("kosa8 Cloud commands take no interactive stdin: write all of a "
                                   "process's input before reading its output or waiting for it")
            self._stdin += data

        async def wait(self) -> int:
            await self._start()
            return await asyncio.shield(self._exit)

        async def poll(self) -> Optional[int]:
            await self._start()
            if not self._exit.done():
                return None
            return await self._exit

        async def _signal(self, sig: str, code: int) -> None:
            self._killed = code
            if not self._started:
                await self._start()
                return
            if self._exit.done():
                return
            try:
                await self._backend._box.exec(["sh", "-c", _SIGNAL, "sh", sig, self._id])
            except Exception as e:  # noqa: BLE001
                raise SandboxError(f"kosa8 kill failed: {e}") from e

        async def terminate(self) -> None:
            await self._signal("TERM", 143)

        async def kill(self) -> None:
            await self._signal("KILL", 137)

    class Kosa8Backend(RuntimeBackend):  # type: ignore[misc, valid-type]
        SUPPORTS_TASK_IMAGES = True
        SUPPORTS_USER_SWITCHING = True
        SUPPORTS_PORT_EXPOSURE = True
        SUPPORTS_STOP_CONFIRMATION = True
        config_class = Kosa8RuntimeConfig

        def __init__(self, config: Any, name: Optional[str] = None) -> None:
            super().__init__(name)
            self.config = config
            self.info = CustomRuntimeInfo(**config.model_dump())
            self._cloud: Optional[AsyncCloud] = None
            self._box: Any = None
            self._box_id: Optional[str] = None
            self._previews: Dict[int, str] = {}
            self._expose_lock = asyncio.Lock()

        @property
        def published_port(self) -> int:
            return SERVICE_PORT

        def _sync_box(self) -> Any:
            if self._box is None or self._cloud is None:
                raise SandboxError("the kosa8 runtime has not started")
            return CloudSandbox(self._cloud.sync(), dict(self._box.info))

        async def start(self) -> None:
            c = self.config
            self._cloud = (cloud_factory or AsyncCloud)()
            kw: Dict[str, Any] = {"lease_s": c.lease_s, "labels": {**dict(c.labels or {}), "verifiers": self.name[:60]}}
            if c.persistent:
                kw["persistent"] = True
            if c.snapshot:
                kw["snapshot"] = c.snapshot
            else:
                for k in ("image", "template", "cpus", "mem_mb", "region"):
                    if getattr(c, k) is not None:
                        kw[k] = getattr(c, k)
                if c.image and c.cpus is None:
                    kw.update(cpus=2, mem_mb=4096)
                if not c.image and not c.template:
                    # verifiers expects sh and Python 3 in the default image.
                    kw["template"] = "python"
            try:
                self._box = await self._cloud.sandboxes.create(**kw)
            except Exception as e:
                raise SandboxError(f"kosa8 create failed: {e}") from e
            self._box_id = self._box.id
            self.info.id = self._box.id
            try:
                from verifiers.v1.runtimes.base import register  # type: ignore

                register(self)
            except ImportError:  # pragma: no cover
                pass
            if c.workdir:
                await self._box.exec(["mkdir", "-p", c.workdir])

        async def exec(self, runtime: Any, argv: List[str], env: Dict[str, str]) -> Any:
            if self._box is None:
                raise SandboxError("the kosa8 runtime has not started")
            return Kosa8Process(self, runtime, argv, runtime.process_env(env))

        async def run_background(self, runtime: Any, argv: List[str], env: Dict[str, str], log: str) -> None:
            """A detached kosa8 background exec, so a long-lived server holds no connection."""
            if self._box is None:
                raise SandboxError("the kosa8 runtime has not started")
            penv = runtime.process_env(env)
            script = f'exec "$@" > {shlex.quote(log)} 2>&1 < /dev/null'
            inner = as_user(["sh", "-c", script, "sh", *argv], getattr(runtime, "user", None), penv)
            try:
                await self._box.exec_background(wrap_argv(inner, self.config.workdir), env=penv or None)
            except Exception as e:
                raise SandboxError(f"kosa8 background exec failed: {e}") from e

        def _abs(self, path: str) -> str:
            return path if path.startswith("/") else f"{(self.config.workdir or '/workspace').rstrip('/')}/{path}"

        async def read(self, runtime: Any, path: str, max_bytes: Optional[int] = None) -> bytes:
            if getattr(runtime, "user", None) is not None:
                return await super().read(runtime, path, max_bytes)  # through the user's own permissions
            try:
                data = await self._box.files.read(self._abs(path))
            except Exception as e:
                raise SandboxError(f"read {path!r}: {e}") from e
            return data if max_bytes is None else data[: max_bytes + 1]

        async def write(self, runtime: Any, path: str, data: bytes) -> None:
            p = self._abs(path)
            try:
                await self._box.files.write(p, data)
                user = getattr(runtime, "user", None)
                if user is not None:
                    r = await self._box.exec(["chown", user, p])
                    if r.exit_code:
                        raise SandboxError(f"chown {user} {p}: {r.stderr.strip()}")
            except SandboxError:
                raise
            except Exception as e:
                raise SandboxError(f"write {path!r}: {e}") from e

        async def expose(self, port: int) -> str:
            """A public kosa8 preview of ``port`` (the server binds 0.0.0.0)."""
            async with self._expose_lock:
                if port not in self._previews:
                    try:
                        p = await self._box.previews.create(port, public=True)
                    except Exception as e:
                        raise SandboxError(f"kosa8 expose port {port} failed: {e}") from e
                    self._previews[port] = p["url"]
                return self._previews[port]

        async def prepare_execution(self, routes: Optional[List[str]], network: Any = None) -> None:
            if not getattr(self, "network_restricted", False):
                return
            cfg = network or self.config
            policy = egress_for(routes, list(cfg.allow), list(cfg.block))
            try:
                await asyncio.to_thread(lambda: self._sync_box().set_egress(**policy))
            except Exception as e:
                raise SandboxError(f"kosa8 egress policy failed: {e}") from e

        async def verify_stopped(self) -> None:
            if self._box_id is None or self._cloud is None:
                return
            try:
                info = (await self._cloud._request("GET", f"/v1/sandboxes/{self._box_id}"))["sandbox"]
            except Kosa8NotFoundError:
                return
            except Exception as e:
                raise SandboxError(f"could not confirm kosa8 sandbox {self._box_id} stopped: {e}") from e
            if info.get("state") not in ("deleted", "deleting", "stopped"):
                raise SandboxError(f"kosa8 sandbox {self._box_id} is still {info.get('state')}")

        def cleanup(self) -> None:
            # Synchronous and idempotent: it is also the atexit backstop.
            box, self._box = self._box, None
            if box is not None and self._cloud is not None:
                _quiet_delete(CloudSandbox(self._cloud.sync(), {"id": box.id}))

    _CLASSES.update(Kosa8RuntimeConfig=Kosa8RuntimeConfig, Kosa8Backend=Kosa8Backend, Kosa8Process=Kosa8Process)
    return _CLASSES


def __getattr__(name: str) -> Any:
    if name in ("Kosa8Backend", "Kosa8RuntimeConfig", "Kosa8Process"):
        return _classes()[name]
    raise AttributeError(name)
