# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""A kosa8 runtime for Prime Intellect ``verifiers`` (v1).

Checked against verifiers' source (``verifiers/v1/runtimes``, 2026-11): a
custom runtime is a ``RuntimeBackend`` subclass named by ``import_path``,
with a ``config_class`` derived from ``CustomRuntimeConfig`` (declaring
``image`` and ``workdir`` because it runs task images). It implements
``start``, ``exec`` (a streaming process), ``run``, ``read``, ``write`` and
a synchronous, idempotent ``cleanup`` that the framework also calls at exit::

    [env.agent.runtime]
    type = "custom"
    import_path = "kosa8.rl.verifiers.Kosa8Backend"
    image = "ghcr.io/acme/task:1.0"     # or template = "python"
    lease_s = 3600

The key comes from ``KOSA8_API_KEY``. kosa8 Cloud runs a buffered command
at most 90 s, so ``exec`` gathers a command's output and streams it when it
ends (no stdin). Each command runs in ``workdir`` (default ``/workspace``).
Not declared (the framework refuses them before provisioning): user
switching, memory/disk limits, compose, port exposure, network policies.
Not run against a live verifiers install in CI; the classes are built on
first use, so importing this module does not need verifiers.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

_CLASSES: Dict[str, Any] = {}

#: Makes the :class:`~kosa8.AsyncCloud` each runtime uses (default: ``AsyncCloud()``,
#: key from ``KOSA8_API_KEY``). Set it for another key or address.
cloud_factory: Optional[Any] = None


def wrap_argv(argv: List[str], workdir: Optional[str]) -> List[str]:
    """``argv`` run in ``workdir`` (a kosa8 Cloud exec takes no cwd)."""
    if not workdir:
        return list(argv)
    return ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", workdir, *argv]


async def _once(data: bytes) -> AsyncIterator[bytes]:
    if data:
        yield data


def _classes() -> Dict[str, Any]:
    if _CLASSES:
        return _CLASSES
    try:
        from verifiers.v1.configs.runtime import CustomRuntimeConfig  # type: ignore
        from verifiers.v1.errors import SandboxError  # type: ignore
        from verifiers.v1.runtimes.backends.custom import CustomRuntimeInfo  # type: ignore
        from verifiers.v1.runtimes.base import ProgramResult, RuntimeBackend, RuntimeProcess  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("kosa8.rl.verifiers needs verifiers v1: pip install verifiers") from e

    from ..aio import AsyncCloud
    from ..cloud import _quiet_delete, CloudSandbox

    class Kosa8RuntimeConfig(CustomRuntimeConfig):  # type: ignore[misc, valid-type]
        image: Optional[str] = None
        workdir: Optional[str] = "/workspace"
        template: Optional[str] = None
        snapshot: Optional[str] = None
        cpus: Optional[int] = None
        mem_mb: Optional[int] = None
        region: Optional[str] = None
        lease_s: int = 3600
        labels: Dict[str, str] = {}

    class Kosa8Process(RuntimeProcess):  # type: ignore[misc, valid-type]
        def __init__(self, task: "asyncio.Task[Any]") -> None:
            self._task = task
            self._out: Optional[Any] = None
            self.stdout = self._stream("stdout")
            self.stderr = self._stream("stderr")

        async def _result(self) -> Any:
            if self._out is None:
                self._out = await self._task
            return self._out

        async def _stream(self, which: str) -> AsyncIterator[bytes]:
            r = await self._result()
            text = getattr(r, which) or ""
            if text:
                yield text.encode()

        async def write(self, data: bytes) -> None:
            raise SandboxError("kosa8 Cloud commands take no stdin")

        async def wait(self) -> int:
            r = await self._result()
            return 1 if r.exit_code is None else int(r.exit_code)

        async def poll(self) -> Optional[int]:
            if not self._task.done():
                return None
            return await self.wait()

        async def terminate(self) -> None:
            self._task.cancel()

        async def kill(self) -> None:
            self._task.cancel()

    class Kosa8Backend(RuntimeBackend):  # type: ignore[misc, valid-type]
        SUPPORTS_TASK_IMAGES = True
        config_class = Kosa8RuntimeConfig

        def __init__(self, config: Any, name: Optional[str] = None) -> None:
            super().__init__(name)
            self.config = config
            self.info = CustomRuntimeInfo(**config.model_dump())
            self._cloud: Optional[AsyncCloud] = None
            self._box: Any = None

        async def start(self) -> None:
            c = self.config
            self._cloud = (cloud_factory or AsyncCloud)()
            kw: Dict[str, Any] = {"lease_s": c.lease_s, "labels": {**dict(c.labels or {}), "verifiers": self.name[:60]}}
            if c.snapshot:
                kw["snapshot"] = c.snapshot
            else:
                for k in ("image", "template", "cpus", "mem_mb", "region"):
                    if getattr(c, k) is not None:
                        kw[k] = getattr(c, k)
                if c.image and c.cpus is None:
                    kw.update(cpus=2, mem_mb=4096)
            try:
                self._box = await self._cloud.sandboxes.create(**kw)
            except Exception as e:
                raise SandboxError(f"kosa8 create failed: {e}") from e
            self.info.id = self._box.id
            try:
                from verifiers.v1.runtimes.base import register  # type: ignore

                register(self)
            except ImportError:  # pragma: no cover
                pass
            if c.workdir:
                await self._box.exec(["mkdir", "-p", c.workdir])

        async def _exec(self, runtime: Any, argv: List[str], env: Dict[str, str]) -> Any:
            if self._box is None:
                raise SandboxError("the kosa8 runtime has not started")
            try:
                return await self._box.exec(wrap_argv(list(argv), self.config.workdir), env=runtime.process_env(env))
            except Exception as e:
                raise SandboxError(f"kosa8 exec failed: {e}") from e

        async def exec(self, runtime: Any, argv: List[str], env: Dict[str, str]) -> Any:
            return Kosa8Process(asyncio.ensure_future(self._exec(runtime, argv, env)))

        async def run(self, runtime: Any, argv: List[str], env: Dict[str, str]) -> Any:
            r = await self._exec(runtime, argv, env)
            return ProgramResult(1 if r.exit_code is None else int(r.exit_code), r.stdout, r.stderr)

        def _abs(self, path: str) -> str:
            return path if path.startswith("/") else f"{(self.config.workdir or '/workspace').rstrip('/')}/{path}"

        async def read(self, runtime: Any, path: str, max_bytes: Optional[int] = None) -> bytes:
            try:
                data = await self._box.files.read(self._abs(path))
            except Exception as e:
                raise SandboxError(f"read {path!r}: {e}") from e
            return data if max_bytes is None else data[: max_bytes + 1]

        async def write(self, runtime: Any, path: str, data: bytes) -> None:
            try:
                await self._box.files.write(self._abs(path), data)
            except Exception as e:
                raise SandboxError(f"write {path!r}: {e}") from e

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


