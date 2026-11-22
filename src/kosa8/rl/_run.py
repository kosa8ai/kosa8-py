# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""RL runs: what a training run did, recorded on kosa8 Cloud and shown on
app.kosa8.com (RL runs)."""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from ..cloud import RUN_LABEL, new_idempotency_key
from ._lease import DEFAULT_LEASE_S, Lease
from ._rollouts import DEFAULT_MASK, MapSpec, RolloutResult, Rollouts, run_map

log = logging.getLogger("kosa8.rl")

#: Rows per ``POST /v1/rl/runs/{id}/rollouts``.
MAX_ROWS_PER_CALL = 500

Row = Union[RolloutResult, Mapping[str, Any]]


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


def _run_of(answer: Any) -> Dict[str, Any]:
    if isinstance(answer, dict) and isinstance(answer.get("run"), dict):
        return answer["run"]
    return answer if isinstance(answer, dict) else {}


def _row(r: Row) -> Dict[str, Any]:
    return r.to_row() if isinstance(r, RolloutResult) else dict(r)


class Run:
    """One RL training run on kosa8 Cloud.

    ::

        with Run(cloud, "my-grpo-run") as run:
            base = cloud.sandboxes.create(template="python")
            results = run.map(base, tasks, solve, grade_cmd=["python", "/grader/test.py"])

    Entering it creates the run (or attaches to ``run_id``) and starts a
    :class:`~kosa8.rl.Lease`: every sandbox this ``cloud`` makes while the
    run is open is labelled ``kosa8.run=<id>`` and leased for ``lease_s``,
    heartbeats keep them alive, and leaving the block (normally, on an
    exception, Ctrl-C or SIGTERM) records what is left, deletes the run's
    sandboxes (``drain_on_exit``) and marks the run ``finished`` or ``failed``.
    """

    def __init__(
        self,
        cloud: Any,
        name: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        project: Optional[str] = None,
        config: Optional[Mapping[str, Any]] = None,
        lease_s: int = DEFAULT_LEASE_S,
        drain_on_exit: bool = True,
        flush_every: int = 100,
        signals: bool = True,
        heartbeat_s: Optional[float] = None,
    ) -> None:
        """``lease_s``: how long the run's sandboxes outlive the last heartbeat
        (sent every ``heartbeat_s``, default a third of it). ``signals``
        installs a SIGTERM handler that drains before exiting (main thread only)."""
        if not name and not run_id:
            raise ValueError("give the run a name, or run_id to attach to one")
        if not 1 <= flush_every <= MAX_ROWS_PER_CALL:
            raise ValueError(f"flush_every must be from 1 to {MAX_ROWS_PER_CALL}")
        self.cloud = cloud
        self.name = name
        self.id: Optional[str] = run_id
        self.project = project
        self.config = dict(config) if config else None
        self.lease_s = lease_s
        self.drain_on_exit = drain_on_exit
        self.flush_every = flush_every
        self.signals = signals
        self.heartbeat_s = heartbeat_s
        #: The run as the API last answered it.
        self.info: Dict[str, Any] = {}
        #: Rollouts sent to the API so far by this object.
        self.recorded = 0
        self.status: Optional[str] = None
        self._buf: List[Dict[str, Any]] = []
        # The chunk being sent and its Idempotency-Key: kept until kosa8 Cloud
        # answers, so a resend (a retry, the next flush) is the same request.
        self._inflight: Optional["tuple[List[Dict[str, Any]], str]"] = None
        self._buf_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._lease: Optional[Lease] = None
        self._prev_defaults: Any = None
        self._step = 0
        self._open = False

    def __repr__(self) -> str:
        return f"Run(id={self.id!r}, name={self.name!r})"

    @classmethod
    def attach(cls, cloud: Any, run_id: str, **kw: Any) -> "Run":
        """A :class:`Run` for an existing run (to keep recording after a restart)."""
        return cls(cloud, run_id=run_id, **kw)

    @staticmethod
    def list(  # noqa: A003
        cloud: Any, *, project: Optional[str] = None, status: Optional[str] = None, name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """The org's runs, newest first (at most ``limit``, up to 100), each
        with a short ``summary`` and its ``recommendations`` count."""
        q = {k: str(v) for k, v in (("project", project), ("status", status), ("name", name), ("limit", limit)) if v}
        r = cloud._request("GET", "/v1/rl/runs" + ("?" + urllib.parse.urlencode(q) if q else "")) or {}
        return list(r.get("runs", []))

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Run":
        """Create (or attach to) the run and start its lease. ``with`` calls this."""
        if self._open:
            return self
        if self.id is None:
            body: Dict[str, Any] = {"name": self.name}
            if self.project:
                body["project"] = self.project
            if self.config:
                body["config"] = self.config
            self.info = _run_of(self.cloud._request("POST", "/v1/rl/runs", body))
            self.id = self.info.get("id")
            if not self.id:
                raise ValueError("kosa8 Cloud answered no run id")
        self.status = "running"
        self._prev_defaults = getattr(self.cloud, "_run_defaults", None)
        self.cloud._run_defaults = {"run_id": self.id, "lease_s": self.lease_s}
        self._lease = Lease(self.cloud, run_id=self.id, lease_s=self.lease_s, drain=self.drain_on_exit,
                            signals=self.signals, on_tick=self._tick, interval_s=self.heartbeat_s)
        self._lease.start()
        self._open = True
        return self

    def __enter__(self) -> "Run":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.finish("failed" if exc_type is not None else "finished")

    def finish(self, status: str = "finished") -> None:
        """Record what is buffered, drain the run's sandboxes (``drain_on_exit``),
        and mark the run ``finished`` (or ``failed``). Safe to call twice."""
        if status not in ("finished", "failed"):
            raise ValueError("status is finished or failed")
        if not self._open:
            return
        self._open = False
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.close()  # final flush (its tick), then the drain
        else:
            self._tick()
        self.cloud._run_defaults = self._prev_defaults
        try:
            self.update(status=status)
        except Exception as e:  # noqa: BLE001 (the rollouts are already saved)
            log.warning("kosa8: could not mark run %s %s: %s", self.id, status, e)
        self.status = status

    @property
    def lease(self) -> Optional[Lease]:
        """The run's lease while it is open."""
        return self._lease

    def _tick(self) -> None:
        try:
            self.flush()
        except Exception as e:  # noqa: BLE001 (kept for the next tick)
            log.warning("kosa8: recording rollouts of run %s failed, will retry: %s", self.id, e)

    def _next_step(self) -> int:
        s = self._step
        self._step += 1
        return s

    def _path(self, suffix: str = "") -> str:
        if not self.id:
            raise ValueError("the run has not started: use `with Run(...)` or run.start()")
        return f"/v1/rl/runs/{_q(self.id)}{suffix}"

    # -- recording -----------------------------------------------------------

    def record(self, rollouts: Union[Row, Iterable[Row]], *, flush: Optional[bool] = None) -> None:
        """Buffer one rollout or many (a :class:`RolloutResult` or a dict with
        ``step``, ``group``, ``index``, ``reward``, ``failure_class``, ``exit``,
        ``duration_ms``, ``tokens``, ``cost_usd``, ``meta``, ``stdout_tail``).
        They are sent in batches (``flush_every``, at most 500 a call), every
        heartbeat, and when the run finishes."""
        rows = [_row(rollouts)] if isinstance(rollouts, (RolloutResult, Mapping)) else [_row(r) for r in rollouts]
        with self._buf_lock:
            self._buf.extend(rows)
            full = len(self._buf) >= self.flush_every
        if flush or (flush is None and full):
            self.flush()

    def flush(self) -> int:
        """Send every buffered rollout now; returns how many were recorded.
        On an error the unsent rows stay buffered and the error is raised.

        Each call of up to 500 rows carries an Idempotency-Key that stays with
        those rows until kosa8 Cloud answers: the client's retries (timeouts,
        5xx) and a later flush resend the same rows with the same key, and
        kosa8 Cloud stores them once."""
        sent = 0
        with self._flush_lock:
            while True:
                if self._inflight is None:
                    with self._buf_lock:
                        chunk, self._buf = self._buf[:MAX_ROWS_PER_CALL], self._buf[MAX_ROWS_PER_CALL:]
                    if not chunk:
                        return sent
                    self._inflight = (chunk, new_idempotency_key())
                chunk, key = self._inflight
                # On an error the chunk stays in flight with its key, for the next flush.
                r = self.cloud._request("POST", self._path("/rollouts"), {"rollouts": chunk},
                                        idempotency_key=key) or {}
                self._inflight = None
                n = int(r.get("recorded", len(chunk)))
                sent += n
                self.recorded += n

    @property
    def pending(self) -> int:
        """Rollouts buffered, not yet sent."""
        with self._buf_lock:
            return len(self._buf) + (len(self._inflight[0]) if self._inflight else 0)

    # -- reading -------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """The run with its summary (rewards, failure classes, live sandboxes, cost, recommendations)."""
        r = self.cloud._request("GET", self._path()) or {}
        self.info = _run_of(r) or self.info
        return r

    def update(self, *, status: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
        body = {k: v for k, v in (("status", status), ("name", name)) if v is not None}
        if not body:
            raise ValueError("give status or name")
        self.info = _run_of(self.cloud._request("PATCH", self._path(), body)) or self.info
        return self.info

    def rollouts(
        self, *, step: Optional[int] = None, failure_class: Optional[str] = None, min_reward: Optional[float] = None,
        max_reward: Optional[float] = None, sandbox_id: Optional[str] = None, limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One page of recorded rollouts (at most 500), oldest first:
        ``{"rollouts": [...], "next_cursor": ...}``. Each has ``source``:
        ``grade`` (recorded by the sealed grader) or ``api`` (by :meth:`record`)."""
        q = {k: str(v) for k, v in (("step", step), ("failure_class", failure_class), ("min_reward", min_reward),
                                     ("max_reward", max_reward), ("sandbox_id", sandbox_id), ("limit", limit),
                                     ("cursor", cursor)) if v is not None}
        return self.cloud._request("GET", self._path("/rollouts" + ("?" + urllib.parse.urlencode(q) if q else ""))) or {}

    def export(self, path: Optional[str] = None) -> bytes:
        """Every rollout as JSON lines; written to ``path`` when given."""
        data = self.cloud._request("GET", self._path("/export?format=jsonl"), raw=True)
        if path is not None:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def drain(self) -> Dict[str, Any]:
        """Delete the run's sandboxes now (the run stays open)."""
        return self.cloud.sandboxes.drain(run_id=self.id)

    # -- rollouts ------------------------------------------------------------

    def map(
        self,
        base: Any,
        tasks: Sequence[Any],
        solve: Callable[[Any, Any], Any],
        *,
        grade_cmd: Optional[Union[str, Sequence[str]]] = None,
        files: Any = None,
        grade_timeout_s: Optional[float] = None,
        grade_env: Optional[Mapping[str, str]] = None,
        reward: Optional[Callable[..., Any]] = None,
        concurrency: int = 16,
        group_size: int = 1,
        step: Optional[int] = None,
        retries: int = 2,
        timeout_s: Optional[float] = None,
        mask: Iterable[str] = DEFAULT_MASK,
        keep: bool = False,
        meta: Optional[Callable[[Any, Any], Mapping[str, Any]]] = None,
    ) -> Rollouts:
        """One training step: ``solve(sandbox, task)`` for every task
        (``group_size`` samples each, e.g. GRPO's G), ``concurrency`` at a time.

        For each rollout: fork ``base`` (or create one: ``base`` may be a
        ``snap_`` id, create options as a dict, or ``make(task)``), run
        ``solve`` in it, grade it in a sealed fork with ``grade_cmd`` and the
        task's hidden files (``task["files"]``: ``{"/grader/test.py": "..."}``;
        or ``files=`` a dict or ``fn(task)``), record it on the run, delete
        the sandbox. Without ``grade_cmd``, ``reward(sandbox, task, output)``
        or ``solve``'s own return (a number, or ``{"reward": x, ...}``) is the reward.

        Infra failures and rate limits are retried ``retries`` times on a
        fresh sandbox, with backoff and jitter (a failed grade is retried on
        its own first). What is still failing in ``mask`` (default infra,
        quota and your own exceptions) comes back ``masked=True`` with
        ``reward=None``, never 0. ``timeout_s`` caps ``solve``. ``step``
        defaults to this run's count of maps. Results are in task order:
        :class:`~kosa8.rl.Rollouts` (``.rewards``, ``.mask``, ``.groups()``).
        """
        if not self._open:
            raise ValueError("the run is not open: use `with Run(...) as run:`")
        spec = MapSpec(grade_cmd=grade_cmd, files=files, grade_timeout_s=grade_timeout_s, grade_env=grade_env,
                       reward=reward, group_size=group_size, step=self._next_step() if step is None else step,
                       retries=retries, timeout_s=timeout_s, mask=tuple(mask), keep=keep, meta=meta)
        return run_map(self, self.cloud, base, tasks, solve, spec, concurrency=concurrency,
                       record=lambda r: self.record(r), run_id=self.id, lease_s=self.lease_s)


class AsyncRun:
    """:class:`Run` for asyncio code, with an :class:`~kosa8.AsyncCloud`::

        async with AsyncRun(cloud, "my-grpo-run") as run:
            base = await cloud.sandboxes.create(template="python")
            results = await run.map(base, tasks, solve, grade_cmd=["python", "/grader/test.py"])

    The lease's heartbeats and the drain run on a thread with the blocking
    client (``cloud.sync()``), so they go on even while the loop is busy and
    still run at interpreter exit.
    """

    def __init__(self, cloud: Any, name: Optional[str] = None, **kw: Any) -> None:
        self.cloud = cloud
        self._run = Run(cloud.sync(), name, **kw)

    def __repr__(self) -> str:
        return f"AsyncRun(id={self.id!r}, name={self.name!r})"

    @property
    def id(self) -> Optional[str]:
        return self._run.id

    @property
    def name(self) -> Optional[str]:
        return self._run.name

    @property
    def lease_s(self) -> int:
        return self._run.lease_s

    @property
    def sync_run(self) -> Run:
        """The blocking :class:`Run` underneath."""
        return self._run

    def _next_step(self) -> int:
        return self._run._next_step()

    async def start(self) -> "AsyncRun":
        await asyncio.to_thread(self._run.start)
        self._prev = getattr(self.cloud, "_run_defaults", None)
        self.cloud._run_defaults = {"run_id": self.id, "lease_s": self.lease_s}
        return self

    async def __aenter__(self) -> "AsyncRun":
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.finish("failed" if exc_type is not None else "finished")

    async def finish(self, status: str = "finished") -> None:
        self.cloud._run_defaults = getattr(self, "_prev", None)
        # Shielded: a cancelled trainer still records and drains.
        await asyncio.shield(asyncio.to_thread(self._run.finish, status))

    async def record(self, rollouts: Union[Row, Iterable[Row]]) -> None:
        """Buffer rollouts; sent in batches off the event loop."""
        self._run.record(rollouts, flush=False)
        if self._run.pending >= self._run.flush_every:
            await asyncio.to_thread(self._run._tick)

    async def flush(self) -> int:
        return await asyncio.to_thread(self._run.flush)

    async def summary(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run.summary)

    async def drain(self) -> Dict[str, Any]:
        return await self.cloud.sandboxes.drain(run_id=self.id)

    async def map(self, base: Any, tasks: Sequence[Any], solve: Callable[[Any, Any], Any], **kw: Any) -> Rollouts:
        """One step under asyncio: ``await solve(sandbox, task)`` per rollout;
        the options are those of :meth:`Run.map` (default ``concurrency=64``)."""
        from ._rollouts import map_rollouts

        return await map_rollouts(solve, tasks, base=base, cloud=self.cloud, run=self, **kw)


__all__ = ["Run", "AsyncRun", "RUN_LABEL"]
