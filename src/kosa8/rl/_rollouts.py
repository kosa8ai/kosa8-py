# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Rollouts: acquire a sandbox, run the agent, grade it, record it, release it."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .._forkmap import ChildPool, ItemTimeout, backoff, call_with_timeout, run_pool
from ..cloud import GradeResult
from ..errors import classify_failure
from .. import retry as _retry
from ..retry import RetryPolicy, acall_with_retry, call_with_retry

#: Failure classes masked by default: the sandbox or the platform failed, not
#: the policy, so the rollout must not train as reward 0. ``error`` is your
#: own ``solve`` raising (a harness bug). Add ``"timeout"`` to mask timeouts
#: too (DeepSWE-style "compact filtering").
DEFAULT_MASK: Tuple[str, ...] = ("infra", "quota", "error")

#: Most of the grader's stdout kept on a result and in a recorded rollout.
STDOUT_TAIL = 64 * 1024


@dataclass
class RolloutResult:
    """One rollout.

    ``masked`` means: leave it out of the gradient (the failure was not the
    policy's). Then ``reward`` is ``None``, never 0.
    """

    task: Any
    #: Which task (its position in ``tasks``) and which sample of its group.
    group: int
    index: int
    step: Optional[int] = None
    reward: Optional[float] = None
    #: ``ok``, ``exit_nonzero`` (the grader ran and failed), ``timeout``, ``infra``, ``quota``, ``client``, ``error``.
    failure_class: str = "ok"
    masked: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    #: What ``solve`` returned.
    output: Any = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    attempts: int = 0
    duration_ms: int = 0
    sandbox_id: Optional[str] = None
    grader_id: Optional[str] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    #: Whether kosa8 Cloud already recorded it (the grader does, given a run).
    recorded: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure_class in ("ok", "exit_nonzero") and not self.masked

    def to_row(self) -> Dict[str, Any]:
        """The row ``POST /v1/rl/runs/{id}/rollouts`` takes."""
        meta = dict(self.meta)
        if self.metrics:
            meta["metrics"] = self.metrics
        if self.masked:
            meta["masked"] = True
        if self.error:
            meta["error"] = self.error[:2000]
        if self.attempts > 1:
            meta["attempts"] = self.attempts
        row: Dict[str, Any] = {
            "step": self.step, "group": self.group, "index": self.index, "sandbox_id": self.sandbox_id,
            "reward": self.reward, "failure_class": self.failure_class, "exit": self.exit_code,
            "duration_ms": self.duration_ms, "meta": meta, "stdout_tail": (self.stdout or "")[-STDOUT_TAIL:],
        }
        if self.tokens is not None:
            row["tokens"] = self.tokens
        if self.cost_usd is not None:
            row["cost_usd"] = self.cost_usd
        return {k: v for k, v in row.items() if v is not None}


class Rollouts(list):
    """The results of a map, in input order (task-major, ``group_size`` per task)."""

    @property
    def rewards(self) -> List[Optional[float]]:
        """Each reward, ``None`` where masked."""
        return [r.reward for r in self]

    @property
    def mask(self) -> List[bool]:
        """``True`` where the rollout should train (not masked)."""
        return [not r.masked for r in self]

    @property
    def mean_reward(self) -> Optional[float]:
        vals = [r.reward for r in self if r.reward is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def by_class(self) -> Dict[str, int]:
        return dict(Counter(r.failure_class for r in self))

    def groups(self) -> List[List[RolloutResult]]:
        """The rollouts of each task together, for group-relative advantages (GRPO)."""
        out: Dict[int, List[RolloutResult]] = {}
        for r in self:
            out.setdefault(r.group, []).append(r)
        return [out[k] for k in sorted(out)]


def task_files(task: Any) -> Optional[Mapping[str, Union[bytes, str]]]:
    """The hidden grader files a task carries: ``task["files"]`` (or ``hidden_files``)."""
    if isinstance(task, Mapping):
        for key in ("files", "hidden_files"):
            v = task.get(key)
            if isinstance(v, Mapping):
                return v
    return None


def task_id(task: Any) -> Any:
    if isinstance(task, Mapping):
        for key in ("id", "task_id", "instance_id"):
            if task.get(key) is not None:
                return task[key]
    return None


def _from_output(res: RolloutResult, out: Any) -> bool:
    """Take reward/metrics/tokens/meta from what ``solve`` returned; True if it gave a reward."""
    if isinstance(out, bool):
        return False
    if isinstance(out, (int, float)):
        res.reward = float(out)
        return True
    if isinstance(out, Mapping):
        if isinstance(out.get("tokens"), int):
            res.tokens = out["tokens"]
        if isinstance(out.get("cost_usd"), (int, float)):
            res.cost_usd = float(out["cost_usd"])
        if isinstance(out.get("meta"), Mapping):
            res.meta.update(out["meta"])
        r = out.get("reward")
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            res.reward = float(r)
            if isinstance(out.get("metrics"), Mapping):
                res.metrics = dict(out["metrics"])
            return True
    return False


def _apply_grade(res: RolloutResult, g: GradeResult, recorded: bool) -> None:
    res.reward = g.reward
    res.metrics = g.metrics
    res.exit_code = g.exit_code
    res.stdout = g.stdout[-STDOUT_TAIL:]
    res.stderr = g.stderr[-STDOUT_TAIL:]
    res.failure_class = g.failure_class
    res.grader_id = g.grader_id
    res.recorded = recorded


_NO_REWARD = (
    "no reward: pass grade_cmd= (a sealed grader), reward=fn(sandbox, task, output), "
    "or return a number or {'reward': x} from solve"
)


@dataclass
class MapSpec:
    """Everything a map needs, shared by the sync and async engines."""

    grade_cmd: Optional[Union[str, Sequence[str]]] = None
    files: Any = None
    grade_timeout_s: Optional[float] = None
    grade_env: Optional[Mapping[str, str]] = None
    reward: Optional[Callable[..., Any]] = None
    group_size: int = 1
    step: Optional[int] = None
    retries: int = 2
    timeout_s: Optional[float] = None
    mask: Tuple[str, ...] = DEFAULT_MASK
    keep: bool = False
    meta: Optional[Callable[[Any, Any], Mapping[str, Any]]] = None

    def check(self) -> None:
        if not isinstance(self.group_size, int) or self.group_size < 1:
            raise ValueError("group_size must be a whole number of at least 1")
        if not isinstance(self.retries, int) or self.retries < 0:
            raise ValueError("retries must be a whole number of at least 0")
        unknown = set(self.mask) - {"ok", "exit_nonzero", "timeout", "infra", "quota", "client", "error"}
        if unknown:
            raise ValueError(f"unknown failure classes in mask: {sorted(unknown)}")

    def files_for(self, task: Any) -> Optional[Mapping[str, Union[bytes, str]]]:
        if callable(self.files):
            return self.files(task)
        if self.files is not None:
            return self.files
        return task_files(task)

    def rollout_info(self, res: RolloutResult) -> Dict[str, Any]:
        info: Dict[str, Any] = {"group": res.group, "index": res.index}
        if res.step is not None:
            info["step"] = res.step
        if res.meta:
            info["meta"] = dict(res.meta)
        return info

    def start(self, tasks: Sequence[Any], i: int, step: Optional[int]) -> RolloutResult:
        ti, g = divmod(i, self.group_size)
        task = tasks[ti]
        res = RolloutResult(task=task, group=ti, index=g, step=step)
        tid = task_id(task)
        if tid is not None:
            res.meta["task"] = tid
        return res

    def finish(self, res: RolloutResult, out: Any, task: Any) -> None:
        if self.meta is not None:
            res.meta.update(self.meta(task, out) or {})
        res.masked = res.failure_class in self.mask
        if res.masked:
            res.reward = None


def _fail(res: RolloutResult, e: BaseException) -> bool:
    """Note a failed attempt; returns whether it may be retried."""
    fc, retryable = classify_failure(e)
    res.failure_class = fc
    res.error = f"{type(e).__name__}: {e}"
    res.reward = None
    return retryable


def run_map(
    run: Any,
    cloud: Any,
    base: Any,
    tasks: Sequence[Any],
    solve: Callable[[Any, Any], Any],
    spec: MapSpec,
    *,
    concurrency: int,
    record: Optional[Callable[[RolloutResult], Any]],
    run_id: Optional[str],
    lease_s: Optional[int],
) -> Rollouts:
    """The sync engine behind :meth:`kosa8.rl.Run.map`."""
    from ..cloud import CloudSandbox

    spec.check()
    tasks = list(tasks)
    n = len(tasks) * spec.group_size
    policy = RetryPolicy(attempts=spec.retries + 1)
    if isinstance(base, CloudSandbox):
        pool = ChildPool(base, run_id=run_id, lease_s=lease_s)
    else:
        pool = ChildPool(make=_maker(cloud, base, run_id, lease_s))
    stop = threading.Event()

    def worker(i: int) -> RolloutResult:
        res = spec.start(tasks, i, spec.step)
        task = tasks[res.group]
        t0 = time.monotonic()
        out = None
        for attempt in range(1, spec.retries + 2):
            res.attempts = attempt
            if stop.is_set():
                res.failure_class, res.error = "error", "cancelled before it ran"
                break
            child = None
            try:
                child = pool.take(task)
                res.sandbox_id = child.id
                c = child
                out = call_with_timeout(solve, (child, task), spec.timeout_s, lambda: pool.release(c))
                res.output = out
                res.failure_class, res.error = "ok", None
                gave = _from_output(res, out)
                if spec.grade_cmd is not None:
                    # The solved state is still there: retry the grader alone, not the agent.
                    gc = child
                    g = call_with_retry(
                        lambda: gc.grade(spec.grade_cmd, files=spec.files_for(task), timeout_s=spec.grade_timeout_s,
                                         env=spec.grade_env, run_id=run_id, rollout=spec.rollout_info(res)),
                        policy, sleep=stop.wait,
                    )
                    _apply_grade(res, g, recorded=bool(run_id))
                elif spec.reward is not None:
                    r = spec.reward(child, task, out)
                    if not _from_output(res, r):
                        raise TypeError("reward= must return a number or {'reward': x}")
                elif not gave:
                    raise ValueError(_NO_REWARD)
                break
            except Exception as e:  # noqa: BLE001 (a rollout's failure is its result)
                retryable = _fail(res, e)
                if not retryable or attempt > spec.retries:
                    break
                if child is not None:
                    pool.release(child)
                    child = None
                backoff(policy, e, attempt, stop)
            finally:
                if child is not None:
                    if spec.keep and res.failure_class not in ("infra",):
                        pool.forget(child)
                    else:
                        pool.release(child)
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        spec.finish(res, out, task)
        if record is not None and not res.recorded:
            try:
                record(res)
            except Exception:  # noqa: BLE001 (recording never fails a rollout)
                pass
        return res

    if n:
        try:
            pool.prefork(min(n, concurrency))
        except BaseException:
            pool.close()
            raise
    return Rollouts(run_pool(n, worker, concurrency, stop, pool.close))


def _maker(cloud: Any, base: Any, run_id: Optional[str], lease_s: Optional[int]) -> Callable[[Any], Any]:
    """How to make each rollout's sandbox when ``base`` is not a sandbox to fork."""
    extra: Dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if lease_s is not None:
        extra["lease_s"] = lease_s
    if callable(base):
        return lambda task: base(task)
    if isinstance(base, str):
        if not base.startswith("snap_"):
            raise ValueError("a string base is a snapshot id (snap_...); pass a sandbox to fork it")
        return lambda task: cloud.sandboxes.create(snapshot=base, **extra)
    if isinstance(base, Mapping):
        kw = {**dict(base), **{k: v for k, v in extra.items() if k not in base}}
        return lambda task: cloud.sandboxes.create(**kw)
    raise TypeError("base is a sandbox to fork, a snap_ id, create options (a dict) or a function of the task")


async def map_rollouts(
    fn: Callable[[Any, Any], Awaitable[Any]],
    tasks: Sequence[Any],
    *,
    base: Any,
    cloud: Any = None,
    run: Any = None,
    grade_cmd: Optional[Union[str, Sequence[str]]] = None,
    files: Any = None,
    grade_timeout_s: Optional[float] = None,
    grade_env: Optional[Mapping[str, str]] = None,
    reward: Optional[Callable[..., Any]] = None,
    concurrency: int = 64,
    group_size: int = 1,
    step: Optional[int] = None,
    retries: int = 2,
    timeout_s: Optional[float] = None,
    mask: Iterable[str] = DEFAULT_MASK,
    keep: bool = False,
    meta: Optional[Callable[[Any, Any], Mapping[str, Any]]] = None,
) -> Rollouts:
    """Run ``await fn(sandbox, task)`` for every task (``group_size`` times
    each), ``concurrency`` at a time, under asyncio.

    ``base`` is an :class:`~kosa8.aio.AsyncCloudSandbox` to fork (cheapest:
    set it up once, fork per rollout), a ``snap_`` id, create options
    (``{"image": "..."}``), or ``async def make(task)`` returning a sandbox.
    Each rollout is graded in a sealed fork with ``grade_cmd`` and the task's
    hidden ``files`` (``task["files"]`` unless ``files=`` says otherwise), or
    by ``reward``, or by what ``fn`` returns. Infra failures and rate limits
    are retried ``retries`` times on a fresh sandbox (backoff with jitter);
    failures in ``mask`` come back ``masked=True`` with ``reward=None``.
    Every sandbox is deleted when its rollout ends, and all of them if the
    map is cancelled. With ``run`` (a :class:`~kosa8.rl.AsyncRun`) each
    rollout is recorded on the run.
    """
    from ..aio import AsyncChildPool, AsyncCloudSandbox

    spec = MapSpec(grade_cmd=grade_cmd, files=files, grade_timeout_s=grade_timeout_s, grade_env=grade_env,
                   reward=reward, group_size=group_size, step=step, retries=retries, timeout_s=timeout_s,
                   mask=tuple(mask), keep=keep, meta=meta)
    spec.check()
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a whole number of at least 1")
    if cloud is None:
        cloud = run.cloud if run is not None else getattr(base, "_cloud", None)
    if cloud is None:
        raise ValueError("give cloud= (an AsyncCloud) when base is not a sandbox")
    run_id = run.id if run is not None else None
    lease_s = run.lease_s if run is not None else None
    tasks = list(tasks)
    n = len(tasks) * spec.group_size
    if spec.step is None and run is not None:
        spec.step = run._next_step()
    policy = RetryPolicy(attempts=spec.retries + 1)
    if isinstance(base, AsyncCloudSandbox):
        pool = AsyncChildPool(base, run_id=run_id, lease_s=lease_s)
    else:
        make = _amaker(cloud, base, run_id, lease_s)
        pool = AsyncChildPool(make=make)
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int) -> RolloutResult:
        res = spec.start(tasks, i, spec.step)
        task = tasks[res.group]
        out = None
        async with sem:
            t0 = time.monotonic()
            for attempt in range(1, spec.retries + 2):
                res.attempts = attempt
                child = None
                try:
                    child = await pool.take(task)
                    res.sandbox_id = child.id
                    try:
                        out = await asyncio.wait_for(fn(child, task), spec.timeout_s)
                    except asyncio.TimeoutError:
                        raise ItemTimeout(spec.timeout_s or 0) from None
                    res.output = out
                    res.failure_class, res.error = "ok", None
                    gave = _from_output(res, out)
                    if spec.grade_cmd is not None:
                        gc = child
                        g = await acall_with_retry(
                            lambda: gc.grade(spec.grade_cmd, files=spec.files_for(task),
                                             timeout_s=spec.grade_timeout_s, env=spec.grade_env, run_id=run_id,
                                             rollout=spec.rollout_info(res)),
                            policy,
                        )
                        _apply_grade(res, g, recorded=bool(run_id))
                    elif spec.reward is not None:
                        r = spec.reward(child, task, out)
                        if asyncio.iscoroutine(r):
                            r = await r
                        if not _from_output(res, r):
                            raise TypeError("reward= must return a number or {'reward': x}")
                    elif not gave:
                        raise ValueError(_NO_REWARD)
                    break
                except Exception as e:  # noqa: BLE001
                    retryable = _fail(res, e)
                    if not retryable or attempt > spec.retries:
                        break
                    if child is not None:
                        await pool.release(child)
                        child = None
                    await _retry._asleep(policy.delay(e, attempt))
                finally:
                    if child is not None:
                        if spec.keep and res.failure_class != "infra":
                            pool.forget(child)
                        else:
                            await pool.release(child)
            res.duration_ms = int((time.monotonic() - t0) * 1000)
        spec.finish(res, out, task)
        if run is not None and not res.recorded:
            try:
                await run.record(res)
            except Exception:  # noqa: BLE001
                pass
        return res

    try:
        if n:
            await pool.prefork(min(n, concurrency))
        return Rollouts(await asyncio.gather(*(one(i) for i in range(n))))
    finally:
        await pool.close()


def _amaker(cloud: Any, base: Any, run_id: Optional[str], lease_s: Optional[int]) -> Callable[[Any], Awaitable[Any]]:
    extra: Dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if lease_s is not None:
        extra["lease_s"] = lease_s
    if callable(base):
        async def call(task: Any) -> Any:
            r = base(task)
            return await r if asyncio.iscoroutine(r) else r
        return call
    if isinstance(base, str):
        if not base.startswith("snap_"):
            raise ValueError("a string base is a snapshot id (snap_...); pass a sandbox to fork it")
        return lambda task: cloud.sandboxes.create(snapshot=base, **extra)
    if isinstance(base, Mapping):
        kw = {**dict(base), **{k: v for k, v in extra.items() if k not in base}}
        return lambda task: cloud.sandboxes.create(**kw)
    raise TypeError("base is a sandbox to fork, a snap_ id, create options (a dict) or a function of the task")
