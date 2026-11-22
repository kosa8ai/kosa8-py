# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""The async kosa8 Cloud client, for rollout loops that run thousands of
sandboxes under asyncio (verifiers, SkyRL, verl agent loops, ...).

>>> import asyncio, kosa8
>>> async def main():
...     async with kosa8.AsyncCloud() as cloud:
...         async with await cloud.sandboxes.create(template="python") as box:
...             print((await box.exec(["python3", "-c", "print(6 * 7)"])).stdout)
>>> asyncio.run(main())

It uses ``httpx`` (``pip install 'kosa8[async]'``) with one pooled HTTP/1.1
client when it is installed, and otherwise runs the standard-library client
in threads, so it works with no dependencies either way. The answers, errors,
retries and checks are those of :class:`kosa8.Kosa8Cloud`.
"""

from __future__ import annotations

import asyncio
import socket
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union

from .cloud import (
    _EXEC_HINT,
    _SETTLED,
    GradeResult,
    Kosa8Cloud,
    Opener,
    SandboxBatch,
    _batch_body,
    _batch_from,
    _create_body,
    _exec_result,
    _fork_body,
    _grade_body,
    _grade_result,
    _is_exec_path,
    _is_int,
    _parse_answer,
    _ScopedRetry,
    _selector,
    _urllib_open,
    _ambient,
    _check_lease,
    _check_key,
    _chunk_key,
    _drain_add,
    _DRAIN_ROUNDS,
    _idem_headers,
    _reindex,
    MAX_FORK,
    MAX_FORK_PER_CALL,
)
from .cloud_features import argv_of, env_list
from .errors import (
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8TimeoutError,
    classify_failure,
)
from . import retry as _retry
from .retry import RetryPolicy, acall_with_retry
from .jobs import AsyncCloudJobs
from .cloud_more import (
    AsyncCloudBurst,
    AsyncCloudEmbeds,
    AsyncCloudPreviewDomains,
    AsyncCloudRecordings,
    AsyncCloudTokens,
    preview_path,
)

#: (method, url, headers, body bytes or None, timeout) -> awaitable of
#: (status, body bytes[, response headers]).
AsyncOpener = Callable[[str, str, Dict[str, str], Optional[bytes], float], Awaitable[Any]]


class _HttpxOpener:
    """One shared ``httpx.AsyncClient``, made on first use (inside the loop)."""

    def __init__(self, max_connections: int) -> None:
        import httpx  # noqa: F401 (checked by the caller)

        self._limits = max_connections
        self._client: Any = None

    async def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=self._limits, max_keepalive_connections=self._limits),
            )
        try:
            r = await self._client.request(method, url, headers=headers, content=body, timeout=timeout)
        except httpx.TimeoutException as e:
            raise TimeoutError(str(e) or "timed out") from e
        except httpx.TransportError as e:
            raise ConnectionError(str(e) or type(e).__name__) from e
        return r.status_code, r.content, dict(r.headers.items())

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()


def _has_httpx() -> bool:
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


class AsyncCloud:
    """The async twin of :class:`kosa8.Kosa8Cloud`: same key, address,
    checks and retries; every call is a coroutine.

    ``async_opener`` replaces the transport (tests); a sync ``opener`` is run
    in threads. With neither, ``httpx`` is used when installed (a pool of
    ``max_connections``), else the standard library in threads.
    Use ``async with AsyncCloud() as cloud`` (or ``await cloud.aclose()``) to
    close the HTTP pool.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = 120.0,
        opener: Optional[Opener] = None,
        async_opener: Optional[AsyncOpener] = None,
        retry: Optional[RetryPolicy] = None,
        max_connections: int = 256,
    ) -> None:
        # The sync client checks the key and address, and is what leases and
        # exit-time cleanup use (they run where no event loop is). Given only
        # an async_opener, it goes through that too (on a loop of its own).
        sync_opener = opener
        if sync_opener is None and async_opener is not None:
            def sync_opener(*a: Any) -> Any:
                return asyncio.run(async_opener(*a))
        self._sync = Kosa8Cloud(api_key, base_url=base_url, env=env, timeout=timeout, opener=sync_opener, retry=retry)
        self.base_url = self._sync.base_url
        self.timeout = timeout
        self.retry = self._sync.retry
        self._httpx: Optional[_HttpxOpener] = None
        if async_opener is not None:
            self._aopen: AsyncOpener = async_opener
        elif opener is None and _has_httpx():
            self._httpx = _HttpxOpener(max_connections)
            self._aopen = self._httpx
        else:
            sync_open = opener or _urllib_open

            async def threaded(*a: Any) -> Any:
                return await asyncio.to_thread(sync_open, *a)

            self._aopen = threaded
        self.sandboxes = AsyncCloudSandboxes(self)
        #: Hosted jobs (see :class:`kosa8.jobs.CloudJobs`).
        self.jobs = AsyncCloudJobs(self)
        #: Burst Passes (see :class:`kosa8.cloud_more.CloudBurst`).
        self.burst = AsyncCloudBurst(self)
        #: Capability tokens (see :class:`kosa8.cloud_more.CloudTokens`).
        self.tokens = AsyncCloudTokens(self)
        #: Desktop recordings (see :class:`kosa8.cloud_more.CloudRecordings`).
        self.recordings = AsyncCloudRecordings(self)

    def __repr__(self) -> str:
        return f"AsyncCloud(base_url={self.base_url!r})"

    def sync(self) -> Kosa8Cloud:
        """A blocking :class:`~kosa8.Kosa8Cloud` with the same key and address."""
        return self._sync

    async def __aenter__(self) -> "AsyncCloud":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._httpx is not None:
            await self._httpx.aclose()

    async def me(self) -> Dict[str, Any]:
        return await self._request("GET", "/v1/me")

    async def usage(self) -> Dict[str, Any]:
        return await self._request("GET", "/v1/usage")

    async def templates(self) -> List[Dict[str, Any]]:
        return (await self._request("GET", "/v1/templates") or {}).get("templates", [])

    def preview_domains(self, preview_id: str) -> AsyncCloudPreviewDomains:
        """A preview's custom domains; see :meth:`kosa8.Kosa8Cloud.preview_domains`."""
        return AsyncCloudPreviewDomains(self, preview_path(preview_id))

    async def fork_map(
        self,
        base: "AsyncCloudSandbox",
        fn: Callable[["AsyncCloudSandbox", Any], Awaitable[Any]],
        items: Sequence[Any],
        *,
        concurrency: int = 16,
        retries: int = 2,
        timeout_s: Optional[float] = None,
        labels: Optional[Mapping[str, str]] = None,
        run_id: Optional[str] = None,
        lease_s: Optional[int] = None,
    ) -> List[Any]:
        """``await fn(child, item)`` for every item in its own fork of ``base``;
        see :meth:`kosa8.Kosa8Cloud.fork_map`. Cancelling it (Ctrl-C under
        ``asyncio.run``) deletes every child before the cancellation goes on."""
        return await afork_map(self, base, fn, items, concurrency=concurrency, retries=retries,
                               timeout_s=timeout_s, labels=labels, run_id=run_id, lease_s=lease_s)

    async def _request(
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
        """See :meth:`kosa8.Kosa8Cloud._request` (the same retries and Idempotency-Key)."""
        extra = _idem_headers(method, path, idempotency_key)
        policy = _ScopedRetry(self.retry if retry is None else retry, method, path, keyed=bool(extra))
        return await acall_with_retry(
            lambda: self._request_once(method, path, body, data=data, raw=raw, extra=extra), policy)

    async def _request_once(self, method: str, path: str, body: Any = None, *, data: Optional[bytes] = None,
                            raw: bool = False, extra: Optional[Mapping[str, str]] = None) -> Any:
        headers, payload = self._sync._headers_payload(body, data, raw, extra)
        try:
            answer = await self._aopen(method, self.base_url + path, headers, payload, self.timeout)
        except (socket.timeout, TimeoutError, asyncio.TimeoutError) as e:
            if _is_exec_path(path):
                raise Kosa8ExecTimeoutError(
                    f"kosa8 Cloud did not answer within {self.timeout} s ({_EXEC_HINT})",
                    method=method, path=path, code="exec_timeout",
                ) from e
            raise Kosa8TimeoutError(f"kosa8 Cloud did not answer within {self.timeout} s", method=method, path=path) from e
        except OSError as e:
            raise Kosa8ConnectionError(f"could not reach kosa8 Cloud at {self.base_url}: {e}", method=method, path=path) from e
        return _parse_answer(answer, method, path, raw)


class AsyncCloudSandboxes:
    def __init__(self, cloud: AsyncCloud) -> None:
        self._cloud = cloud

    async def create(
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
        record: Optional[bool] = None,
        wait: bool = True,
        wait_timeout: float = 300.0,
        idempotency_key: Optional[str] = None,
    ) -> "AsyncCloudSandbox":
        """Start a sandbox; see :meth:`kosa8.CloudSandboxes.create`. Use it as
        ``async with await cloud.sandboxes.create(...) as box:`` to delete it on the way out."""
        body = _create_body(
            self._cloud, template=template, region=region, sleep_after_s=sleep_after_s, ttl_s=ttl_s,
            delete_after=delete_after, accept_price=accept_price, size=size, cpus=cpus, mem_mb=mem_mb, image=image,
            disk_gb=disk_gb, persistent=persistent, docker=docker, cpu=cpu, name=name, env=env, snapshot=snapshot,
            egress=egress, secrets=secrets, labels=labels, lease_s=lease_s, run_id=run_id, record=record,
        )
        r = await self._cloud._request("POST", "/v1/sandboxes", body, idempotency_key=idempotency_key)
        box = AsyncCloudSandbox(self._cloud, r["sandbox"])
        try:
            if wait and box.state not in _SETTLED:
                await box.wait_until_running(timeout=wait_timeout)
        except BaseException:
            await _aquiet_delete(box)
            raise
        return box

    async def create_many(
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
        """``n`` (1 to 1000) sandboxes in one call; see :meth:`kosa8.CloudSandboxes.create_many`."""
        amb = _ambient(self._cloud)
        body = _batch_body(
            n, template=template, image=image, cpus=cpus, mem_mb=mem_mb, region=region, labels=labels,
            ttl_s=ttl_s, lease_s=amb.get("lease_s") if lease_s is None else lease_s,
            run_id=amb.get("run_id") if run_id is None else run_id, partial_ok=partial_ok, prefix=prefix,
        )
        r = await self._cloud._request("POST", "/v1/sandboxes/batch", body, idempotency_key=idempotency_key) or {}
        batch = _batch_from(self._cloud, r, AsyncCloudSandbox, n)
        if wait:
            await _await_all(batch, wait_timeout, partial_ok)
        return batch

    async def get(self, sandbox_id: str) -> "AsyncCloudSandbox":
        r = await self._cloud._request("GET", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id, safe='')}")
        return AsyncCloudSandbox(self._cloud, r["sandbox"])

    async def list(  # noqa: A003
        self, *, labels: Optional[Mapping[str, Union[str, bool]]] = None, limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> List["AsyncCloudSandbox"]:
        """One page of the org's sandboxes, newest first, optionally with every label in ``labels``."""
        q: List[tuple] = []
        if limit is not None:
            if not _is_int(limit) or limit < 1:
                raise ValueError("limit must be a positive whole number")
            q.append(("limit", str(limit)))
        if cursor:
            q.append(("cursor", cursor))
        for k, v in (labels or {}).items():
            q.append(("label", k if v is True else f"{k}={v}"))
        r = await self._cloud._request("GET", "/v1/sandboxes" + ("?" + urllib.parse.urlencode(q) if q else "")) or {}
        return [AsyncCloudSandbox(self._cloud, s) for s in r.get("sandboxes", [])]

    async def heartbeat(
        self, *, run_id: Optional[str] = None, labels: Optional[Mapping[str, str]] = None,
        ids: Optional[Sequence[Any]] = None, extend_s: Optional[int] = None,
    ) -> int:
        """Push back leases; see :meth:`kosa8.CloudSandboxes.heartbeat`."""
        body = _selector(run_id, labels, ids)
        if extend_s is not None:
            _check_lease(extend_s)
            body["extend_s"] = extend_s
        return int((await self._cloud._request("POST", "/v1/sandboxes/heartbeat", body) or {}).get("extended", 0))

    async def drain(
        self, *, run_id: Optional[str] = None, labels: Optional[Mapping[str, str]] = None,
        ids: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        """Delete every matching sandbox; see :meth:`kosa8.CloudSandboxes.drain`."""
        sel = _selector(run_id, labels, ids)
        out: Dict[str, Any] = {"deleted": 0, "ids": [], "failed": []}
        for _ in range(_DRAIN_ROUNDS):
            r = await self._cloud._request("POST", "/v1/sandboxes/drain", sel) or {}
            if not _drain_add(out, r):
                break
        return out

    async def grade(self, sandbox: Union[str, "AsyncCloudSandbox"], cmd: Union[str, Sequence[str]], **kw: Any) -> GradeResult:
        """Grade a solved sandbox in a sealed fork; see :meth:`kosa8.CloudSandbox.grade`."""
        box = sandbox if isinstance(sandbox, AsyncCloudSandbox) else AsyncCloudSandbox(self._cloud, {"id": sandbox})
        return await box.grade(cmd, **kw)


class AsyncCloudSandbox:
    """One cloud sandbox, async. ``info`` is its state as of the last call."""

    def __init__(self, cloud: AsyncCloud, info: Dict[str, Any]) -> None:
        self._cloud = cloud
        self.info = info

    def __repr__(self) -> str:
        return f"AsyncCloudSandbox(id={self.id!r}, state={self.state!r})"

    async def __aenter__(self) -> "AsyncCloudSandbox":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await _aquiet_delete(self)

    @property
    def id(self) -> str:
        return self.info["id"]

    @property
    def state(self) -> str:
        return self.info.get("state", "")

    @property
    def files(self) -> "AsyncCloudFiles":
        return AsyncCloudFiles(self)

    @property
    def embeds(self) -> AsyncCloudEmbeds:
        """Signed, expiring iframe URLs for its terminal or desktop; see :class:`kosa8.cloud_more.CloudEmbeds`."""
        return AsyncCloudEmbeds(self._cloud, self._path())

    def _path(self, suffix: str = "") -> str:
        return f"/v1/sandboxes/{urllib.parse.quote(self.id, safe='')}{suffix}"

    async def exec(self, cmd: Union[str, Sequence[str]], *, image: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None):
        """Run a command and wait for it; see :meth:`kosa8.CloudSandbox.exec`."""
        body: Dict[str, Any] = {"cmd": argv_of(cmd)}
        if image is not None:
            body["image"] = image
        el = env_list(env)
        if el:
            body["env"] = el
        return _exec_result(await self._cloud._request("POST", self._path("/exec"), body))

    async def refresh(self) -> "AsyncCloudSandbox":
        self.info = (await self._cloud._request("GET", self._path()))["sandbox"]
        return self

    async def delete(self) -> None:
        await self._cloud._request("DELETE", self._path())

    async def sleep(self) -> "AsyncCloudSandbox":
        self.info = (await self._cloud._request("POST", self._path("/sleep")))["sandbox"]
        return self

    async def wake(self) -> "AsyncCloudSandbox":
        self.info = (await self._cloud._request("POST", self._path("/wake")))["sandbox"]
        return self

    async def wait_until_running(self, *, timeout: float = 300.0, interval: float = 1.0) -> "AsyncCloudSandbox":
        deadline = time.monotonic() + timeout
        while self.state not in _SETTLED:
            if time.monotonic() + interval > deadline:
                raise Kosa8TimeoutError(f"sandbox {self.id} still {self.state} after {timeout} s")
            await asyncio.sleep(interval)
            await self.refresh()
        if self.state == "error":
            raise Kosa8Error(f"sandbox {self.id} failed to start: {self.info.get('last_error') or 'no reason given'}")
        return self

    async def fork(
        self, n: int = 1, *, labels: Optional[Mapping[str, str]] = None, run_id: Optional[str] = None,
        lease_s: Optional[int] = None, partial_ok: Optional[bool] = None, idempotency_key: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> SandboxBatch:
        """``n`` live copies; see :meth:`kosa8.CloudSandbox.fork`."""
        _check_key(idempotency_key)
        if not _is_int(n) or n < 1:
            raise ValueError("n must be a whole number of at least 1")
        amb = _ambient(self._cloud)
        run_id = amb.get("run_id") if run_id is None else run_id
        lease_s = amb.get("lease_s") if lease_s is None else lease_s
        out = SandboxBatch(requested=n, run_id=run_id)
        try:
            left, call = n, 0
            while left > 0:
                k = min(left, MAX_FORK_PER_CALL)
                r = await self._cloud._request("POST", self._path("/fork"),
                                               _fork_body(k, labels, run_id, lease_s, partial_ok, prefix),
                                               idempotency_key=_chunk_key(idempotency_key, call)) or {}
                part = _batch_from(self._cloud, r, AsyncCloudSandbox, k)
                _reindex(part.failed, n - left)
                out.extend(part)
                out.failed.extend(part.failed)
                left -= k
                call += 1
        except BaseException:
            await asyncio.gather(*(_aquiet_delete(c) for c in out))
            raise
        return out

    async def grade(
        self, cmd: Union[str, Sequence[str]], *, files: Optional[Mapping[str, Union[bytes, str]]] = None,
        timeout_s: Optional[float] = None, env: Optional[Mapping[str, str]] = None, keep: bool = False,
        run_id: Optional[str] = None, rollout: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> GradeResult:
        """Grade in a sealed fork with hidden ``files``; see :meth:`kosa8.CloudSandbox.grade`."""
        amb = _ambient(self._cloud)
        run_id = amb.get("run_id") if run_id is None else run_id
        body = _grade_body(cmd, files=files, timeout_s=timeout_s, env=env, keep=keep, run_id=run_id, rollout=rollout)
        return _grade_result(await self._cloud._request("POST", self._path("/grade"), body,
                                                        idempotency_key=idempotency_key) or {})

    async def heartbeat(self, *, extend_s: Optional[int] = None) -> int:
        return await self._cloud.sandboxes.heartbeat(ids=[self.id], extend_s=extend_s)

    async def set_labels(self, labels: Mapping[str, str]) -> Dict[str, str]:
        r = await self._cloud._request("PUT", self._path("/labels"), {"labels": dict(labels)}) or {}
        self.info = {**self.info, "labels": r.get("labels", {})}
        return r.get("labels", {})

    async def checkpoint(self, label: Optional[str] = None) -> Dict[str, Any]:
        body = {"label": label} if label is not None else {}
        return (await self._cloud._request("POST", self._path("/checkpoints"), body))["checkpoint"]

    async def rewind(self, checkpoint_id: str) -> Dict[str, Any]:
        """Go back to a checkpoint; answers the API's dict (``checkpoint``, ``backup``, ``sandbox``)."""
        if not checkpoint_id:
            raise ValueError("rewind needs a checkpoint id")
        r = await self._cloud._request("POST", self._path("/rewind"), {"checkpoint_id": checkpoint_id}) or {}
        if isinstance(r.get("sandbox"), dict):
            self.info = r["sandbox"]
        return r

    async def snapshot(self, name: Optional[str] = None) -> Dict[str, Any]:
        body = {"name": name} if name is not None else {}
        return (await self._cloud._request("POST", self._path("/snapshots"), body))["snapshot"]

    async def recordings(self) -> List[Dict[str, Any]]:
        """Its desktop recordings; see :meth:`kosa8.CloudSandbox.recordings`."""
        return (await self._cloud._request("GET", self._path("/recordings")) or {}).get("recordings", [])


class AsyncCloudFiles:
    """Files in a cloud sandbox (at most 8 MiB each); see :class:`kosa8.CloudFiles`."""

    def __init__(self, sandbox: AsyncCloudSandbox) -> None:
        self._sb = sandbox

    def _q(self, suffix: str, path: str, **extra: str) -> str:
        if not path:
            raise ValueError("a path is required, e.g. /workspace/app.py")
        return self._sb._path(suffix) + "?" + urllib.parse.urlencode({"path": path, **extra})

    async def read(self, path: str) -> bytes:
        return await self._sb._cloud._request("GET", self._q("/files", path), raw=True)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return (await self.read(path)).decode(encoding)

    async def write(self, path: str, content: Union[bytes, str], encoding: str = "utf-8") -> Dict[str, Any]:
        data = content.encode(encoding) if isinstance(content, str) else bytes(content)
        return (await self._sb._cloud._request("PUT", self._q("/files", path), data=data) or {}).get("file", {})

    async def list(self, path: str = "/workspace") -> List[Dict[str, Any]]:  # noqa: A003
        return (await self._sb._cloud._request("GET", self._q("/files/list", path)) or {}).get("files", [])

    async def mkdir(self, path: str) -> Dict[str, Any]:
        if not path:
            raise ValueError("a path is required, e.g. /workspace/data")
        r = await self._sb._cloud._request("POST", self._sb._path("/files/mkdir"), {"path": path}) or {}
        return r.get("file", {})

    async def delete(self, path: str, *, recursive: bool = False) -> None:
        extra = {"recursive": "1"} if recursive else {}
        await self._sb._cloud._request("DELETE", self._q("/files", path, **extra))


async def _aquiet_delete(box: AsyncCloudSandbox) -> None:
    """Delete, shielded from cancellation, ignoring a sandbox already gone."""
    try:
        await asyncio.shield(box._cloud._request("DELETE", box._path()))
    except Kosa8NotFoundError:
        pass
    except BaseException:  # noqa: BLE001 (cleanup is best effort; its lease covers the rest)
        pass


async def _await_all(batch: SandboxBatch, timeout: float, partial_ok: bool) -> None:
    async def one(i: int, box: AsyncCloudSandbox) -> Optional[AsyncCloudSandbox]:
        try:
            await box.wait_until_running(timeout=timeout)
            return box
        except Kosa8Error as e:
            if not partial_ok:
                raise
            await _aquiet_delete(box)
            fc, _ = classify_failure(e)
            batch.failed.append({"index": i, "id": box.id, "code": e.code or "not_ready", "message": str(e),
                                 "failure_class": "infra" if fc == "error" else fc, "retryable": True})
            return None

    try:
        done = await asyncio.gather(*(one(i, b) for i, b in enumerate(list(batch))))
    except BaseException:
        await asyncio.gather(*(_aquiet_delete(b) for b in batch))
        raise
    batch[:] = [b for b in done if b is not None]


class AsyncChildPool:
    """The async :class:`kosa8._forkmap.ChildPool`."""

    def __init__(self, base: Optional[AsyncCloudSandbox] = None, *, labels: Optional[Mapping[str, str]] = None,
                 run_id: Optional[str] = None, lease_s: Optional[int] = None,
                 make: Optional[Callable[[Any], Awaitable[AsyncCloudSandbox]]] = None,
                 sync_cloud: Optional[Kosa8Cloud] = None) -> None:
        from ._forkmap import track

        if (base is None) == (make is None):
            raise ValueError("give a base sandbox to fork, or make to create each sandbox")
        self.base = base
        self.make = make
        self._sync_cloud = sync_cloud
        self.labels = dict(labels) if labels else None
        self.run_id = run_id
        self.lease_s = lease_s
        self._ready: List[AsyncCloudSandbox] = []
        self._live: Dict[str, AsyncCloudSandbox] = {}
        self.closed = False
        track(self)

    async def _fork(self, n: int, partial_ok: Optional[bool]) -> List[AsyncCloudSandbox]:
        from ._forkmap import Cancelled, failure_from_entry

        kids = await self.base.fork(n, labels=self.labels, run_id=self.run_id, lease_s=self.lease_s,
                                    partial_ok=partial_ok)
        if self.closed:
            await asyncio.gather(*(_aquiet_delete(k) for k in kids))
            raise Cancelled()
        for k in kids:
            self._live[k.id] = k
        if not kids:
            failed = getattr(kids, "failed", None) or [{"message": "the fork made no sandbox"}]
            raise failure_from_entry(failed[0])
        return list(kids)

    async def prefork(self, n: int) -> None:
        if self.make is not None:
            return
        left = n
        while left > 0 and not self.closed:
            k = min(left, MAX_FORK)
            try:
                self._ready.extend(await self._fork(k, True))
            except Exception:  # noqa: BLE001 (workers fork one by one and see the error)
                return
            left -= k

    async def take(self, arg: Any = None) -> AsyncCloudSandbox:
        from ._forkmap import Cancelled

        if self.closed:
            raise Cancelled()
        if self.make is not None:
            child = await self.make(arg)
            if self.closed:
                await _aquiet_delete(child)
                raise Cancelled()
            self._live[child.id] = child
            return child
        if self._ready:
            return self._ready.pop()
        return (await self._fork(1, None))[0]

    def forget(self, child: AsyncCloudSandbox) -> None:
        self._live.pop(child.id, None)

    async def release(self, child: AsyncCloudSandbox) -> None:
        if self._live.pop(child.id, None) is not None:
            await _aquiet_delete(child)

    async def close(self) -> None:
        self.closed = True
        kids, self._live = list(self._live.values()), {}
        self._ready.clear()
        if not kids:
            return
        cloud = kids[0]._cloud
        try:
            await asyncio.shield(cloud.sandboxes.drain(ids=[k.id for k in kids]))
            return
        except BaseException:  # noqa: BLE001
            pass
        await asyncio.gather(*(_aquiet_delete(k) for k in kids))

    def close_sync(self) -> None:
        """The exit backstop: delete what is left with the blocking client."""
        kids, self._live = list(self._live.values()), {}
        if kids:
            kids[0]._cloud.sync().sandboxes.drain(ids=[k.id for k in kids])


async def afork_map(
    cloud: AsyncCloud,
    base: AsyncCloudSandbox,
    fn: Callable[[AsyncCloudSandbox, Any], Awaitable[Any]],
    items: Sequence[Any],
    *,
    concurrency: int = 16,
    retries: int = 2,
    timeout_s: Optional[float] = None,
    labels: Optional[Mapping[str, str]] = None,
    run_id: Optional[str] = None,
    lease_s: Optional[int] = None,
) -> List[Any]:
    from ._forkmap import ItemTimeout
    from .cloud import ForkResult

    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a whole number of at least 1")
    if not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a whole number of at least 0")
    items = list(items)
    policy = RetryPolicy(attempts=retries + 1)
    pool = AsyncChildPool(base, labels=labels, run_id=run_id, lease_s=lease_s)
    sem = asyncio.Semaphore(concurrency)

    async def one(item: Any) -> ForkResult:
        res = ForkResult(item=item)
        t0 = time.monotonic()
        async with sem:
            for attempt in range(1, retries + 2):
                res.attempts = attempt
                child = None
                try:
                    child = await pool.take()
                    res.sandbox_id = child.id
                    try:
                        res.value = await asyncio.wait_for(fn(child, item), timeout_s)
                    except asyncio.TimeoutError:
                        raise ItemTimeout(timeout_s or 0) from None
                    res.error, res.failure_class = None, "ok"
                    break
                except Exception as e:  # noqa: BLE001 (reported in the result)
                    res.value, res.error = None, e
                    fc, retryable = classify_failure(e)
                    res.failure_class = fc
                    if not retryable or attempt > retries:
                        break
                    if child is not None:
                        await pool.release(child)
                        child = None
                    await _retry._asleep(policy.delay(e, attempt))
                finally:
                    if child is not None:
                        await pool.release(child)
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    try:
        if items:
            await pool.prefork(min(len(items), concurrency))
        return list(await asyncio.gather(*(one(it) for it in items)))
    finally:
        await pool.close()
