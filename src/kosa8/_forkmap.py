# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""The thread-pool engine behind :meth:`Kosa8Cloud.fork_map` and
:meth:`kosa8.rl.Run.map`: hand out forks of a base sandbox, run work in them,
retry what can pass, and never leave a child behind (Ctrl-C included)."""

from __future__ import annotations

import atexit
import queue
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from .errors import Kosa8ApiError, Kosa8Error, Kosa8TimeoutError, classify_failure
from .retry import RetryPolicy

#: Children forked ahead of the workers in one call, at most.
_PREFORK_MAX = 100

#: Pools with children that may still be alive, for the exit backstop.
_POOLS: "weakref.WeakSet[Any]" = weakref.WeakSet()
_armed = False


def track(pool: Any) -> None:
    """Delete ``pool``'s children at interpreter exit if nothing else did
    (a second Ctrl-C can cut a ``finally`` short). ``pool.close_sync()`` must
    be synchronous: the event loop is gone by then."""
    global _armed
    _POOLS.add(pool)
    if not _armed:
        _armed = True
        atexit.register(_backstop)


def _backstop() -> None:
    for pool in list(_POOLS):
        try:
            pool.close_sync()
        except Exception:  # noqa: BLE001 (best effort at exit)
            pass


class Cancelled(Kosa8Error):
    """The map was interrupted before this item ran."""

    def __init__(self, message: str = "cancelled before it ran") -> None:
        super().__init__(message, body={"failure_class": "error", "retryable": False})


class ItemTimeout(Kosa8TimeoutError):
    """An item ran past the map's ``timeout_s``; its sandbox was deleted."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            f"gave up after {timeout_s} s; its sandbox was deleted",
            body={"failure_class": "timeout", "retryable": False},
            code="timeout",
        )


def failure_from_entry(entry: Mapping[str, Any]) -> Kosa8ApiError:
    """A ``failed[]`` entry of a batch or fork answer as an exception."""
    return Kosa8ApiError(
        str(entry.get("message") or entry.get("code") or "not created"),
        code=entry.get("code"),
        body={"failure_class": entry.get("failure_class") or "infra", "retryable": bool(entry.get("retryable", True)),
              **dict(entry)},
    )


def call_with_timeout(fn: Callable[..., Any], args: Sequence[Any], timeout_s: Optional[float],
                      on_timeout: Callable[[], Any]) -> Any:
    """``fn(*args)``; past ``timeout_s`` run ``on_timeout`` (delete the
    sandbox, so the abandoned call fails fast) and raise :class:`ItemTimeout`."""
    if timeout_s is None:
        return fn(*args)
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 (handed to the caller)
            box["error"] = e

    t = threading.Thread(target=target, name="kosa8-item", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        on_timeout()
        raise ItemTimeout(timeout_s)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class ChildPool:
    """Forks of ``base`` for workers to take. Every child it made and has not
    released is tracked, so :meth:`close` deletes them all, even mid-call."""

    def __init__(self, base: Any = None, *, labels: Optional[Mapping[str, str]] = None, run_id: Optional[str] = None,
                 lease_s: Optional[int] = None, make: Optional[Callable[[Any], Any]] = None) -> None:
        if (base is None) == (make is None):
            raise ValueError("give a base sandbox to fork, or make to create each sandbox")
        self.base = base
        #: Instead of forking: ``make(arg)`` creates the sandbox for ``take(arg)``.
        self.make = make
        self.labels = dict(labels) if labels else None
        self.run_id = run_id
        self.lease_s = lease_s
        self._ready: "queue.Queue[Any]" = queue.Queue()
        self._live: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.closed = False
        track(self)

    def close_sync(self) -> None:
        self.close()

    def _adopt(self, kids: List[Any]) -> None:
        with self._lock:
            closed = self.closed
            if not closed:
                for k in kids:
                    self._live[k.id] = k
        if closed:
            for k in kids:
                _delete(k)
            raise Cancelled()

    def _fork(self, n: int, partial_ok: Optional[bool]) -> List[Any]:
        kids = self.base.fork(n, labels=self.labels, run_id=self.run_id, lease_s=self.lease_s, partial_ok=partial_ok)
        with self._lock:
            closed = self.closed
            if not closed:
                for k in kids:
                    self._live[k.id] = k
        if closed:
            for k in kids:
                _delete(k)
            raise Cancelled()
        if not kids:
            failed = getattr(kids, "failed", None) or [{"message": "the fork made no sandbox"}]
            raise failure_from_entry(failed[0])
        return list(kids)

    def prefork(self, n: int) -> None:
        """Fork up to ``n`` children ahead, in calls of at most 100. Failures
        here are ignored: each worker then forks its own and sees the error."""
        if self.make is not None:
            return
        left = n
        while left > 0 and not self.closed:
            k = min(left, _PREFORK_MAX)
            try:
                for child in self._fork(k, True):
                    self._ready.put(child)
            except Exception:  # noqa: BLE001 (workers retry one by one)
                return
            left -= k

    def take(self, arg: Any = None) -> Any:
        if self.closed:
            raise Cancelled()
        if self.make is not None:
            child = self.make(arg)
            self._adopt([child])
            return child
        try:
            return self._ready.get_nowait()
        except queue.Empty:
            return self._fork(1, None)[0]

    def release(self, child: Any) -> None:
        """Delete a child taken from the pool (once; later calls do nothing)."""
        with self._lock:
            mine = self._live.pop(child.id, None) is not None
        if mine:
            _delete(child)

    def forget(self, child: Any) -> None:
        """Stop tracking a child without deleting it (``keep=True``)."""
        with self._lock:
            self._live.pop(child.id, None)

    def live_ids(self) -> Set[str]:
        with self._lock:
            return set(self._live)

    def close(self) -> None:
        """Delete every child not yet released (queued or in use)."""
        with self._lock:
            self.closed = True
            kids = list(self._live.values())
            self._live.clear()
        while True:
            try:
                self._ready.get_nowait()
            except queue.Empty:
                break
        if not kids:
            return
        cloud = getattr(kids[0], "_cloud", None)
        try:
            # One call for the lot; then one by one for anything it missed.
            cloud.sandboxes.drain(ids=[k.id for k in kids])
            return
        except Exception:  # noqa: BLE001
            pass
        for k in kids:
            _delete(k)


def _delete(box: Any) -> None:
    try:
        box.delete()
    except Exception:  # noqa: BLE001 (gone already, or its lease deletes it)
        pass


def run_pool(n: int, worker: Callable[[int], Any], concurrency: int, stop: threading.Event,
             cleanup: Callable[[], Any]) -> List[Any]:
    """``worker(i)`` for i in range(n) on ``concurrency`` threads, results in
    order. On any exception in the waiting thread (Ctrl-C), ``stop`` is set,
    queued items are dropped and ``cleanup`` runs before it is re-raised."""
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a whole number of at least 1")
    results: List[Any] = [None] * n
    if n == 0:
        cleanup()
        return results
    ex = ThreadPoolExecutor(max_workers=min(concurrency, n), thread_name_prefix="kosa8")
    try:
        futs = {ex.submit(worker, i): i for i in range(n)}
        for f in as_completed(futs):
            results[futs[f]] = f.result()
    except BaseException:
        stop.set()
        ex.shutdown(wait=False, cancel_futures=True)
        cleanup()
        raise
    ex.shutdown(wait=True)
    cleanup()
    return results


def backoff(policy: RetryPolicy, exc: BaseException, attempt: int, stop: threading.Event) -> None:
    """Wait before the next attempt; returns early when ``stop`` is set."""
    stop.wait(policy.delay(exc, attempt))


def fork_map(
    cloud: Any,
    base: Any,
    fn: Callable[[Any, Any], Any],
    items: Sequence[Any],
    *,
    concurrency: int = 16,
    retries: int = 2,
    timeout_s: Optional[float] = None,
    labels: Optional[Mapping[str, str]] = None,
    run_id: Optional[str] = None,
    lease_s: Optional[int] = None,
    policy: Optional[RetryPolicy] = None,
) -> List[Any]:
    from .cloud import ForkResult

    if not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a whole number of at least 0")
    items = list(items)
    policy = policy or RetryPolicy(attempts=retries + 1)
    pool = ChildPool(base, labels=labels, run_id=run_id, lease_s=lease_s)
    stop = threading.Event()

    def worker(i: int) -> ForkResult:
        res = ForkResult(item=items[i])
        t0 = time.monotonic()
        for attempt in range(1, retries + 2):
            res.attempts = attempt
            if stop.is_set():
                res.error, res.failure_class = Cancelled(), "error"
                break
            child = None
            try:
                child = pool.take()
                res.sandbox_id = child.id
                c = child
                res.value = call_with_timeout(fn, (child, items[i]), timeout_s, lambda: pool.release(c))
                res.error, res.failure_class = None, "ok"
                break
            except Exception as e:  # noqa: BLE001 (reported in the result)
                res.value, res.error = None, e
                fc, retryable = classify_failure(e)
                res.failure_class = fc
                if not retryable or attempt > retries:
                    break
                if child is not None:
                    pool.release(child)
                    child = None
                backoff(policy, e, attempt, stop)
            finally:
                if child is not None:
                    pool.release(child)
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    if items:
        try:
            pool.prefork(min(len(items), concurrency))
        except BaseException:
            pool.close()
            raise
    return run_pool(len(items), worker, concurrency, stop, pool.close)
