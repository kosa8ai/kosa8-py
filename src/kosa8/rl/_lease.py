# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Leases: sandboxes that die with the trainer."""

from __future__ import annotations

import atexit
import logging
import signal
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

log = logging.getLogger("kosa8.rl")

#: Default lease: a sandbox is deleted this long after the last heartbeat.
DEFAULT_LEASE_S = 600


class Lease:
    """Keeps a set of leased sandboxes alive while the trainer is, and deletes
    them when it stops.

    A background thread sends a heartbeat every ``interval_s`` (default a
    third of ``lease_s``) for the sandboxes picked by ``run_id``, ``labels``
    or ``ids``. On :meth:`close` (the end of ``with``, an exception, Ctrl-C,
    ``SIGTERM`` or interpreter exit) the thread stops and, with ``drain``,
    every matching sandbox is deleted. If the process is killed outright
    (``SIGKILL``, a lost machine) the heartbeats stop and kosa8 Cloud deletes
    them ``lease_s`` later. Sandboxes must be created with ``lease_s`` (or
    inside a :class:`~kosa8.rl.Run`) for the server to expire them.
    """

    def __init__(
        self,
        cloud: Any,
        *,
        run_id: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ids: Optional[Sequence[str]] = None,
        lease_s: int = DEFAULT_LEASE_S,
        interval_s: Optional[float] = None,
        drain: bool = True,
        signals: bool = True,
        on_tick: Optional[Callable[[], Any]] = None,
    ) -> None:
        if not (run_id or labels or ids):
            raise ValueError("a lease needs run_id, labels or ids: which sandboxes")
        if not isinstance(lease_s, int) or isinstance(lease_s, bool) or lease_s < 60:
            raise ValueError("lease_s must be a whole number of seconds, at least 60")
        self.cloud = cloud.sync() if hasattr(cloud, "sync") else cloud
        self.selector: Dict[str, Any] = {"run_id": run_id, "labels": dict(labels) if labels else None,
                                         "ids": list(ids) if ids else None}
        self.lease_s = lease_s
        self.interval_s = float(interval_s) if interval_s is not None else max(5.0, lease_s / 3.0)
        self.drain = drain
        self.signals = signals
        self.on_tick = on_tick
        #: Heartbeats sent, and the last error a heartbeat or tick raised.
        self.beats = 0
        self.last_error: Optional[BaseException] = None
        #: What the drain answered on close (``{"deleted": n, "failed": [...]}``).
        self.drained: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._lock = threading.Lock()
        self._prev_term: Any = None

    def __enter__(self) -> "Lease":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> "Lease":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, name="kosa8-lease", daemon=True)
        self._thread.start()
        atexit.register(self.close)
        if self.signals and threading.current_thread() is threading.main_thread():
            try:
                self._prev_term = signal.signal(signal.SIGTERM, self._on_term)
            except (ValueError, OSError):  # not allowed here (embedded interpreters)
                self._prev_term = None
        return self

    def beat(self) -> int:
        """Send one heartbeat now; returns how many sandboxes it extended."""
        n = self.cloud.sandboxes.heartbeat(**{k: v for k, v in self.selector.items() if v}, extend_s=self.lease_s)
        self.beats += 1
        return n

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.beat()
            except Exception as e:  # noqa: BLE001 (keep beating; the lease has slack)
                self.last_error = e
                log.warning("kosa8 lease heartbeat failed: %s", e)
            if self.on_tick is not None:
                try:
                    self.on_tick()
                except Exception as e:  # noqa: BLE001
                    self.last_error = e
                    log.warning("kosa8 lease tick failed: %s", e)

    def _on_term(self, signum: int, frame: Any) -> None:
        self.close()
        prev = self._prev_term
        if callable(prev):
            prev(signum, frame)
        raise SystemExit(128 + signum)

    def close(self) -> Optional[Dict[str, Any]]:
        """Stop the heartbeats and, with ``drain``, delete every matching
        sandbox. Safe to call more than once and from any thread."""
        with self._lock:
            if self._closed:
                return self.drained
            self._closed = True
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        try:
            atexit.unregister(self.close)
        except Exception:  # noqa: BLE001
            pass
        if self._prev_term is not None and threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, self._prev_term)
            except (ValueError, OSError, TypeError):
                pass
            self._prev_term = None
        if self.on_tick is not None:
            try:
                self.on_tick()
            except Exception as e:  # noqa: BLE001
                self.last_error = e
                log.warning("kosa8 lease final tick failed: %s", e)
        if self.drain:
            try:
                self.drained = self.cloud.sandboxes.drain(**{k: v for k, v in self.selector.items() if v})
            except Exception as e:  # noqa: BLE001 (the server's lease expiry is the backstop)
                self.last_error = e
                log.warning("kosa8 drain failed (leased sandboxes still expire in %s s): %s", self.lease_s, e)
        return self.drained

    @property
    def closed(self) -> bool:
        return self._closed


def lease(
    cloud: Any,
    *,
    run_id: Optional[str] = None,
    labels: Optional[Mapping[str, str]] = None,
    ids: Optional[Sequence[str]] = None,
    lease_s: int = DEFAULT_LEASE_S,
    interval_s: Optional[float] = None,
    drain: bool = True,
    signals: bool = True,
) -> Lease:
    """A :class:`Lease`; use it with ``with``::

        with kosa8.rl.lease(cloud, labels={"job": "exp-7"}):
            boxes = cloud.sandboxes.create_many(256, labels={"job": "exp-7"}, lease_s=600)
            ...
        # every sandbox labelled job=exp-7 is gone, even after a crash
    """
    return Lease(cloud, run_id=run_id, labels=labels, ids=ids, lease_s=lease_s, interval_s=interval_s,
                 drain=drain, signals=signals)
