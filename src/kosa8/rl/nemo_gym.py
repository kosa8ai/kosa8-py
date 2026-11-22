# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 as a NeMo Gym sandbox provider.

Checked against NeMo Gym's source (``nemo_gym/sandbox/providers/base.py``
and the E2B provider, 2026-11). A provider is any class with a ``name`` and
the async methods of the ``SandboxProvider`` protocol: ``create(spec) ->
SandboxHandle``, ``exec(handle, command, *, cwd, env, timeout_s, user) ->
SandboxExecResult``, ``upload_file``, ``download_file``, ``status``,
``close`` and ``aclose``; this one also has the optional snapshot methods
(``snapshot``, ``create_from_snapshot``, ``wait_snapshot_ready``,
``delete_snapshot``, ``list_snapshots``) and ``write_file``/``read_file``.
NeMo Gym builds it from config as ``Kosa8Provider(**kwargs)`` and finds it
through this package's ``nemo_gym.sandbox_providers`` entry point::

    sandbox:
      kosa8:
        api_key: ${oc.env:KOSA8_API_KEY,null}
        create: {template: python, lease_s: 3600}
        exec: {default_timeout_s: 85}

How a ``SandboxSpec`` maps: ``image`` boots that OCI image (default size
2 vCPU / 4 GiB, or ``resources.cpu``/``memory_mib``), ``ttl_s`` is the
sandbox's ttl (at least 60 s), ``env`` goes to every command, ``files`` are
written before ``create`` returns, ``metadata`` become labels (at most 16),
``workdir`` is each command's default cwd. ``entrypoint``, ``ports``,
``sidecars``, ``user`` and GPUs are refused with ``ValueError`` (NeMo Gym's
rule for values a provider cannot honor). A non-zero exit is a normal
result; a sandbox that is gone answers ``return_code=125`` with
``error_type="sandbox"`` like the built-in providers; a command past its
limit raises ``TimeoutError``. kosa8 Cloud runs a buffered command at most
90 s. Not run against a live NeMo Gym install in CI.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..errors import Kosa8Error, Kosa8ExecTimeoutError, Kosa8NotFoundError, classify_failure

#: What NeMo Gym's own providers answer when the sandbox, not the command, failed.
SANDBOX_RUNTIME_RETURN_CODE = 125


def _types() -> Any:
    """NeMo Gym's provider types, or look-alikes when it is not installed."""
    try:
        from nemo_gym.sandbox.providers import base  # type: ignore

        return base.SandboxHandle, base.SandboxExecResult, base.SandboxStatus, getattr(base, "SandboxSnapshotInfo", None)
    except ImportError:
        return _Handle, _ExecResult, _Status, _SnapshotInfo


@dataclass
class _Handle:
    sandbox_id: str
    provider_name: str
    raw: Any


@dataclass(frozen=True)
class _ExecResult:
    stdout: Optional[str]
    stderr: Optional[str]
    return_code: int
    error_type: Optional[str] = None


class _Status(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _SnapshotInfo:
    snapshot_id: str
    name: Optional[str]
    state: Optional[str] = None
    sandbox_id: Optional[str] = None


_STATES = {
    "running": "RUNNING", "creating": "STARTING", "starting": "STARTING", "waking": "STARTING",
    "sleeping": "PAUSED", "paused": "PAUSED", "stopped": "STOPPED", "deleted": "STOPPED",
    "error": "ERROR", "host_lost": "ERROR", "host_removed": "ERROR",
}


@dataclass
class _Raw:
    box: Any
    workdir: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)


def _label_key(k: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_./" else "-" for ch in str(k))[:63]


class Kosa8Provider:
    """The kosa8 NeMo Gym sandbox provider; see the module docstring."""

    name = "kosa8"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        create: Optional[Mapping[str, Any]] = None,
        exec: Optional[Mapping[str, Any]] = None,  # noqa: A002 (NeMo Gym's config key)
        cloud: Any = None,
    ) -> None:
        if cloud is None:
            from ..aio import AsyncCloud

            cloud = AsyncCloud(api_key, base_url=base_url)
        self._cloud = cloud
        self._create = dict(create or {})
        self._exec = dict(exec or {})
        unknown = set(self._create) - {"template", "cpus", "mem_mb", "region", "lease_s", "labels", "ttl_s"}
        if unknown:
            raise ValueError(f"unknown kosa8 create options: {sorted(unknown)}")

    # -- lifecycle -----------------------------------------------------------

    def _create_kwargs(self, spec: Any) -> Dict[str, Any]:
        for attr in ("entrypoint", "ports", "sidecars"):
            if getattr(spec, attr, None):
                raise ValueError(f"the kosa8 provider does not support SandboxSpec.{attr}")
        if getattr(spec, "user", None) is not None:
            raise ValueError("the kosa8 provider does not switch users")
        kw: Dict[str, Any] = {k: v for k, v in self._create.items() if k != "labels"}
        res = getattr(spec, "resources", None)
        if res is not None and getattr(res, "gpu", None):
            raise ValueError("the kosa8 provider does not attach GPUs through NeMo Gym yet")
        image = getattr(spec, "image", None)
        if image:
            kw.pop("template", None)
            kw["image"] = image
        cpu = getattr(res, "cpu", None) if res is not None else None
        mem = getattr(res, "memory_mib", None) if res is not None else None
        if cpu or mem:
            kw["cpus"] = max(1, int(round(cpu or 2)))
            kw["mem_mb"] = int(mem or 4096)
        elif image and "cpus" not in kw:
            kw.update(cpus=2, mem_mb=4096)
        ttl = getattr(spec, "ttl_s", None)
        if ttl:
            kw["ttl_s"] = max(60, int(ttl))
        labels = dict(self._create.get("labels") or {})
        for k, v in (getattr(spec, "metadata", None) or {}).items():
            labels[_label_key(k)] = str(v)[:200]
        if len(labels) > 16:
            labels = dict(list(labels.items())[:16])
        if labels:
            kw["labels"] = labels
        if getattr(spec, "env", None):
            kw["env"] = dict(spec.env)
        if getattr(spec, "ready_timeout_s", None):
            kw["wait_timeout"] = float(spec.ready_timeout_s)
        return kw

    async def _finish(self, box: Any, spec: Any) -> Any:
        Handle, _, _, _ = _types()
        raw = _Raw(box=box, workdir=getattr(spec, "workdir", None), env=dict(getattr(spec, "env", None) or {}))
        try:
            if raw.workdir:
                await box.exec(["mkdir", "-p", raw.workdir])
            for path, data in (getattr(spec, "files", None) or {}).items():
                await box.files.write(path, data)
        except BaseException:
            await self._delete(box)
            raise
        return Handle(sandbox_id=box.id, provider_name=self.name, raw=raw)

    async def create(self, spec: Any) -> Any:
        """A running sandbox for ``spec``, its files written."""
        box = await self._cloud.sandboxes.create(**self._create_kwargs(spec))
        return await self._finish(box, spec)

    async def close(self, handle: Any) -> None:
        await self._delete(handle.raw.box)

    async def _delete(self, box: Any) -> None:
        try:
            await asyncio.shield(box.delete())
        except Kosa8NotFoundError:
            pass

    async def aclose(self) -> None:
        aclose = getattr(self._cloud, "aclose", None)
        if aclose is not None:
            await aclose()

    async def status(self, handle: Any) -> Any:
        _, _, Status, _ = _types()
        try:
            await handle.raw.box.refresh()
        except Kosa8NotFoundError:
            return Status.STOPPED
        except Kosa8Error:
            return Status.UNKNOWN
        return getattr(Status, _STATES.get(handle.raw.box.state, "UNKNOWN"))

    # -- commands and files --------------------------------------------------

    async def exec(
        self,
        handle: Any,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_s: Optional[float] = None,
        user: Optional[Any] = None,
    ) -> Any:
        _, ExecResult, _, _ = _types()
        if user is not None:
            raise ValueError("the kosa8 provider does not switch users")
        raw: _Raw = handle.raw
        where = cwd or raw.workdir
        script = f"cd {shlex.quote(where)} && {command}" if where else command
        timeout = timeout_s if timeout_s is not None else self._exec.get("default_timeout_s")
        try:
            call = raw.box.exec(["sh", "-c", script], env={**raw.env, **(env or {})} or None)
            r = await (asyncio.wait_for(call, timeout) if timeout else call)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"kosa8 command did not finish within {timeout} s") from e
        except Kosa8ExecTimeoutError as e:
            raise TimeoutError(f"kosa8 command ran past its limit: {e}") from e
        except Kosa8Error as e:
            fc, _ = classify_failure(e)
            if fc in ("infra", "client"):
                return ExecResult(stdout=None, stderr=f"kosa8 sandbox failed: {e}",
                                  return_code=SANDBOX_RUNTIME_RETURN_CODE, error_type="sandbox")
            raise
        return ExecResult(stdout=r.stdout, stderr=r.stderr,
                          return_code=SANDBOX_RUNTIME_RETURN_CODE if r.exit_code is None else int(r.exit_code))

    async def write_file(self, handle: Any, target_path: str, data: Any) -> None:
        await handle.raw.box.files.write(target_path, data)

    async def read_file(self, handle: Any, source_path: str) -> bytes:
        return await handle.raw.box.files.read(source_path)

    async def upload_file(self, handle: Any, source_path: Path, target_path: str) -> None:
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"Source file not found: {src}")
        await self.write_file(handle, target_path, src.read_bytes())

    async def download_file(self, handle: Any, source_path: str, target_path: Path) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await self.read_file(handle, source_path))

    # -- snapshots (NeMo Gym's optional SupportsSandboxSnapshot) -------------

    async def snapshot(self, handle: Any, *, name: Optional[str] = None) -> str:
        snap = await handle.raw.box.snapshot(name)
        return snap["id"]

    async def wait_snapshot_ready(self, snapshot_id: str) -> None:
        return None  # a kosa8 snapshot is usable when snapshot() returns

    async def delete_snapshot(self, snapshot_id: str) -> None:
        await asyncio.to_thread(self._cloud.sync().snapshots.delete, snapshot_id)

    async def list_snapshots(self, prefix: str) -> List[Any]:
        _, _, _, Info = _types()
        snaps = await asyncio.to_thread(self._cloud.sync().snapshots.list)
        out = []
        for s in snaps or []:
            nm = s.get("name")
            if prefix and not (nm or "").startswith(prefix):
                continue
            out.append(Info(snapshot_id=s["id"], name=nm, state=s.get("state"), sandbox_id=s.get("sandbox_id")))
        return out

    async def create_from_snapshot(self, snapshot_id: str, spec: Any) -> Any:
        kw = self._create_kwargs(spec)
        for k in ("template", "image", "cpus", "mem_mb", "region"):
            kw.pop(k, None)
        box = await self._cloud.sandboxes.create(snapshot=snapshot_id, **kw)
        return await self._finish(box, spec)
