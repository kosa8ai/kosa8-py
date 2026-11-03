"""Checkpoints: client-side bookkeeping over daemon snapshots, exactly as pkg/checkpoint.

A checkpoint is a snapshot with the id ``<sandbox>-ckpt-<n>``; an ordered stack
of them per sandbox records labels and order. The daemon has no checkpoint
endpoint. The default store is the CLI's own ``~/.kosa8/checkpoints.json``,
locked with the same ``flock`` on ``checkpoints.json.lock`` the Go tools take,
so checkpoints taken here are seen by ``kosa8 rewind`` and the other way round.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from .errors import Kosa8CheckpointError

#: How many checkpoints are kept per sandbox; the oldest is evicted (pkg/checkpoint MaxPerSandbox).
MAX_CHECKPOINTS_PER_SANDBOX = 10

T = TypeVar("T")
StoreData = Dict[str, List[Dict[str, Any]]]


def validate_sandbox_name(name: str) -> None:
    """Reject a name that could escape the store (pkg/checkpoint validSandbox)."""
    if name in ("", ".", "..") or "/" in name or "\\" in name or name.startswith("."):
        raise Kosa8CheckpointError(f"invalid sandbox name {json.dumps(name)}")


class MemoryCheckpointStore:
    """Keeps stacks in memory only; nothing is shared with the kosa8 CLI."""

    def __init__(self) -> None:
        self._data: StoreData = {}
        self._mu = threading.Lock()

    def load(self) -> StoreData:
        with self._mu:
            return copy.deepcopy(self._data)

    def update(self, fn: Callable[[StoreData], T]) -> T:
        with self._mu:
            data = copy.deepcopy(self._data)
            out = fn(data)
            self._data = data
            return out


class FileCheckpointStore:
    """The store the kosa8 CLI, MCP server and desk use (mode 0600, rewritten atomically)."""

    def __init__(self, path: Optional[str] = None, *, lock_timeout: float = 10.0) -> None:
        self.path = path or os.path.join(os.path.expanduser("~"), ".kosa8", "checkpoints.json")
        #: Seconds to wait for checkpoints.json.lock before giving up (10, as @kosa8/sdk).
        self.lock_timeout = lock_timeout
        self._mu = threading.Lock()

    def load(self) -> StoreData:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}  # a corrupt store must not make the sandbox unusable (as pkg/checkpoint)
        return parsed if isinstance(parsed, dict) else {}

    def update(self, fn: Callable[[StoreData], T]) -> T:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        with self._mu:
            fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    import fcntl

                    deadline = time.monotonic() + self.lock_timeout
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise Kosa8CheckpointError(
                                    f"timed out after {self.lock_timeout:g}s waiting for {self.path}.lock; "
                                    "another kosa8 process is holding it"
                                ) from None
                            time.sleep(0.05)
                except ImportError:  # Windows: in-process lock only, as pkg/checkpoint lock_other.go
                    pass
                data = self.load()
                out = fn(data)
                tmp = self.path + ".tmp"
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self.path)
                return out
            finally:
                os.close(fd)  # closing drops the flock


def _seq_of(cid: str, sandbox: str) -> int:
    pre = sandbox + "-ckpt-"
    if not cid.startswith(pre):
        return 0
    m = re.match(r"^(\d+)", cid[len(pre):])
    return int(m.group(1)) if m else 0


_FRAC = re.compile(r"\.(\d+)")


def _ts(c: Dict[str, Any]) -> float:
    # Go writes RFC 3339 with up to nine fractional digits; Python before 3.11
    # takes exactly six and no "Z", so normalise both before parsing.
    s = (c.get("created") or "").replace("Z", "+00:00")
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _sorted(lst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for _, _, c in sorted(((_ts(c), i, c) for i, c in enumerate(lst)), key=lambda x: (x[0], x[1]))]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CheckpointBook:
    """The stack logic of pkg/checkpoint, over any store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def next_id(self, sandbox: str) -> Tuple[str, List[str]]:
        """The snapshot id the next checkpoint should use, and ids to evict to stay under the cap."""
        validate_sandbox_name(sandbox)
        lst = self.store.load().get(sandbox, [])
        n = len(lst) + 1
        for c in lst:
            v = _seq_of(c.get("id", ""), sandbox)
            if v >= n:
                n = v + 1
        over = len(lst) + 1 - MAX_CHECKPOINTS_PER_SANDBOX
        evict = [c["id"] for c in lst[:over]] if over > 0 else []
        return f"{sandbox}-ckpt-{n}", evict

    def record(self, cid: str, sandbox: str, label: str = "", evicted: Optional[List[str]] = None) -> Dict[str, Any]:
        validate_sandbox_name(sandbox)
        rec = {"id": cid, "sandbox": sandbox, "label": label or "", "created": _now()}
        drop = set(evicted or [])

        def fn(s: StoreData) -> None:
            s[sandbox] = [e for e in s.get(sandbox, []) if e.get("id") not in drop] + [rec]

        self.store.update(fn)
        return rec

    def list(self, sandbox: str) -> List[Dict[str, Any]]:
        return _sorted(list(self.store.load().get(sandbox, [])))

    def latest(self, sandbox: str) -> Dict[str, Any]:
        lst = self.list(sandbox)
        if not lst:
            raise Kosa8CheckpointError(
                f"no checkpoints for {json.dumps(sandbox)} — take one with checkpoint() or `kosa8 checkpoint {sandbox}`"
            )
        return lst[-1]

    def resolve(self, sandbox: str, ref: str) -> Dict[str, Any]:
        """Find by id or (case-insensitive) label, newest match first."""
        for c in reversed(self.list(sandbox)):
            if c.get("id") == ref or (c.get("label") or "").lower() == ref.lower():
                return c
        raise Kosa8CheckpointError(f"no checkpoint {json.dumps(ref)} for sandbox {json.dumps(sandbox)}")

    def pop(self, sandbox: str, cid: str) -> List[str]:
        """Keep ``cid`` as the top of the stack; return the ids taken after it."""

        def fn(s: StoreData) -> List[str]:
            lst = s.get(sandbox, [])
            idx = next((i for i, c in enumerate(lst) if c.get("id") == cid), -1)
            if idx < 0:
                raise Kosa8CheckpointError(f"no checkpoint {json.dumps(cid)} for sandbox {json.dumps(sandbox)}")
            orphaned = [c["id"] for c in lst[idx + 1:]]
            s[sandbox] = lst[: idx + 1]
            return orphaned

        return self.store.update(fn)

    def forget(self, sandbox: str) -> List[str]:
        """Drop every checkpoint of a sandbox; return the snapshot ids to delete."""

        def fn(s: StoreData) -> List[str]:
            ids = [c["id"] for c in s.get(sandbox, [])]
            s.pop(sandbox, None)
            return ids

        return self.store.update(fn)
