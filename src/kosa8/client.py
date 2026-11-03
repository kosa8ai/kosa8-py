"""Typed client for the kosa8 native API. Mirrors ``@kosa8/sdk`` (client.ts) method for method."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from .checkpoints import CheckpointBook, FileCheckpointStore, _seq_of
from .errors import Kosa8CheckpointError, Kosa8ConflictError, Kosa8Error, Kosa8StreamError, Kosa8TimeoutError
from .transport import Body, Transport, enc

Env = Union[Mapping[str, str], Sequence[str], None]
Chunk = Callable[[bytes], None]
EgressPolicy = Union[str, Mapping[str, Sequence[str]], None]

_TRANSIENT = re.compile(
    r"connection refused|EOF|connection reset|broken pipe|no such file or directory|"
    r"stream ended|ECONNREFUSED|ECONNRESET|unreachable",
    re.I,
)


@dataclass
class ExecResult:
    """The outcome of run/exec/attach. A non-zero ``exit_code`` is not an error."""

    exit_code: int
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class ExploreBranch:
    name: str
    variant: str
    exit: int = 0
    output: str = ""
    duration_ms: int = 0
    error: Optional[str] = None


@dataclass
class ExploreResult:
    parent: str
    branches: List[ExploreBranch] = field(default_factory=list)
    winner: str = ""
    removed: int = 0


def _egress_string(egress: EgressPolicy, allow: Optional[Sequence[str]]) -> Optional[str]:
    if allow:
        if egress is not None and egress != "deny":
            raise Kosa8Error("egress_allow already limits the network to the hosts it lists; do not also set egress")
        return "allow:" + ",".join(h.strip() for h in allow)
    if egress is None:
        return None
    if isinstance(egress, str):
        return egress
    hosts = list(egress.get("allow", []))
    if not hosts:
        raise Kosa8Error('an egress allow-list needs at least one host; use egress="deny" for no network')
    return "allow:" + ",".join(h.strip() for h in hosts)


def _env_list(env: Env) -> Optional[List[str]]:
    if env is None:
        return None
    if isinstance(env, Mapping):
        return [f"{k}={v}" for k, v in env.items()]
    return list(env)


class Kosa8Client:
    """Typed client for the kosa8 native API over the daemon's unix socket (or a remote worker).

    >>> from kosa8 import Kosa8Client
    >>> k = Kosa8Client()
    >>> sb = k.create_sandbox(name="demo", egress="deny")["sandbox"]

    Connection options follow the CLI: ``KOSA8_HOST``/``KOSA8_TOKEN`` select a
    remote worker, otherwise ``~/.kosa8/run/kosa8.sock``.
    """

    def __init__(
        self,
        *,
        socket_path: Optional[str] = None,
        host: Optional[str] = None,
        token: Optional[str] = None,
        insecure: bool = False,
        actor: str = "tool",
        agent_token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        checkpoint_store: Any = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.transport = Transport(
            socket_path=socket_path,
            host=host,
            token=token,
            insecure=insecure,
            actor=actor,
            agent_token=agent_token,
            headers=headers,
            timeout=timeout,
            env=env,
        )
        self.checkpoints = CheckpointBook(checkpoint_store or FileCheckpointStore())
        self._stdin_sent: set = set()
        self._stdin_mu = threading.Lock()

    # --- status ---------------------------------------------------------------

    def health(self, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """GET /kosa8/v1/health: daemon status and version."""
        return self.transport.json("GET", "/kosa8/v1/health", approval=approval)

    def version(self) -> str:
        return self.health()["version"]

    # --- host engine ------------------------------------------------------------

    def pull(self, ref: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST /kosa8/v1/pull: pull an image into the host engine."""
        return self.transport.json("POST", "/kosa8/v1/pull", body={"ref": ref}, approval=approval)

    # --- sandboxes --------------------------------------------------------------

    def list_sandboxes(self) -> List[Dict[str, Any]]:
        """GET /kosa8/v1/sandboxes"""
        return self.transport.json("GET", "/kosa8/v1/sandboxes") or []

    def get_sandbox(self, id_or_name: str) -> Optional[Dict[str, Any]]:
        """Find one sandbox by id or name (from the list; there is no single-sandbox GET)."""
        lst = self.list_sandboxes()
        return next((s for s in lst if s.get("id") == id_or_name), None) or next(
            (s for s in lst if s.get("name") == id_or_name), None
        )

    def create_sandbox(
        self,
        *,
        name: Optional[str] = None,
        cpus: Optional[int] = None,
        mem_mb: Optional[int] = None,
        disk_gb: Optional[int] = None,
        mounts: Optional[List[Dict[str, Any]]] = None,
        egress: EgressPolicy = None,
        egress_allow: Optional[Sequence[str]] = None,
        capture: Optional[Dict[str, Any]] = None,
        labels: Optional[Mapping[str, str]] = None,
        broker: Optional[Dict[str, Any]] = None,
        approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes: boot a microVM sandbox.

        ``egress``: ``"deny"``, ``"any"``, ``"fakenet"``, ``"allow:a.com,b.com"`` or
        ``{"allow": [...]}``. ``egress_allow=[...]`` is shorthand for the last.
        When omitted the daemon applies the org policy's default.
        """
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if cpus:
            body["cpus"] = cpus
        if mem_mb:
            body["mem_mb"] = mem_mb
        if disk_gb:
            body["disk_gb"] = disk_gb
        if mounts:
            body["mounts"] = mounts
        e = _egress_string(egress, egress_allow)
        if e is not None:
            body["egress"] = e
        if capture:
            body["capture"] = capture
        if labels:
            body["labels"] = dict(labels)
        if broker:
            body["broker"] = broker
        return self.transport.json("POST", "/kosa8/v1/sandboxes", body=body, approval=approval)

    def start_sandbox(self, sandbox: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes/{id}/up: boot a stopped sandbox from its own disk."""
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/up", approval=approval)

    def stop_sandbox(self, sandbox: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes/{id}/down: stop without deleting (disk kept)."""
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/down", approval=approval)

    def delete_sandbox(
        self, sandbox: str, *, wipe: bool = False, keep_checkpoints: bool = False, approval: Optional[str] = None
    ) -> Dict[str, Any]:
        """DELETE /kosa8/v1/sandboxes/{id}; its checkpoints go too unless ``keep_checkpoints``."""
        out = self.transport.json(
            "DELETE", f"/kosa8/v1/sandboxes/{enc(sandbox)}", query={"wipe": wipe}, approval=approval
        ) or {}
        dropped = 0
        if not keep_checkpoints:
            try:
                ids = self.checkpoints.forget(sandbox)
                for sid in ids:
                    try:
                        self.delete_snapshot(sid, wipe=wipe, approval=approval)
                    except Kosa8Error:
                        pass
                dropped = len(ids)
            except Kosa8Error:
                pass  # bookkeeping debris, not a failed delete
        return {**out, "checkpoints_dropped": dropped}

    def pull_into_sandbox(self, sandbox: str, ref: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes/{id}/pull: pull an image into the sandbox's private engine."""
        return self.transport.json(
            "POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/pull", body={"ref": ref}, approval=approval
        )

    def sandbox_images(self, sandbox: str) -> List[str]:
        return self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/images") or []

    def sandbox_containers(self, sandbox: str) -> List[Dict[str, Any]]:
        return self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/containers") or []

    def sandbox_egress(self, sandbox: str) -> Dict[str, Any]:
        """GET /kosa8/v1/sandboxes/{id}/egress: the policy the host enforces and what it decided."""
        return self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/egress")

    # --- running code -------------------------------------------------------------

    def run(
        self,
        sandbox: str,
        image: str,
        args: Sequence[str] = (),
        *,
        env: Env = None,
        on_stdout: Optional[Chunk] = None,
        on_stderr: Optional[Chunk] = None,
        on_status: Optional[Chunk] = None,
        timeout: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> ExecResult:
        """POST /kosa8/v1/sandboxes/{id}/run: run a container to completion in the sandbox's engine.

        The image must already be in the sandbox (``pull_into_sandbox``).
        """
        body = {"image": image, "args": list(args)}
        e = _env_list(env)
        if e is not None:
            body["env"] = e
        frames = self.transport.frames(
            "POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/run", body=body, approval=approval, timeout=timeout
        )
        return _collect(frames, on_stdout, on_stderr, on_status)

    def start_container(
        self, sandbox: str, image: str, args: Sequence[str] = (), *, approval: Optional[str] = None
    ) -> str:
        """POST /kosa8/v1/sandboxes/{id}/start: start a detached container; returns its id."""
        out = self.transport.json(
            "POST",
            f"/kosa8/v1/sandboxes/{enc(sandbox)}/start",
            body={"image": image, "args": list(args)},
            approval=approval,
        )
        return out["id"]

    def exec(
        self,
        sandbox: str,
        container: str,
        args: Sequence[str],
        *,
        on_stdout: Optional[Chunk] = None,
        on_stderr: Optional[Chunk] = None,
        timeout: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> ExecResult:
        """POST /kosa8/v1/sandboxes/{id}/exec: run a command in a running container of the sandbox."""
        return _collect(
            self.exec_frames(sandbox, container, args, timeout=timeout, approval=approval), on_stdout, on_stderr, None
        )

    def exec_frames(
        self,
        sandbox: str,
        container: str,
        args: Sequence[str],
        *,
        timeout: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """The raw frame stream of an exec, for callers that process output as it arrives."""
        return self.transport.frames(
            "POST",
            f"/kosa8/v1/sandboxes/{enc(sandbox)}/exec",
            body={"container": container, "args": list(args)},
            approval=approval,
            timeout=timeout,
        )

    def start_interactive(
        self,
        sandbox: str,
        image: str,
        args: Sequence[str] = (),
        *,
        env: Env = None,
        tty: bool = False,
        approval: Optional[str] = None,
    ) -> str:
        """POST /kosa8/v1/sandboxes/{id}/start-interactive: start a container with stdin held open."""
        out = self.transport.json(
            "POST",
            f"/kosa8/v1/sandboxes/{enc(sandbox)}/start-interactive",
            body={"image": image, "args": list(args), "env": _env_list(env), "tty": tty},
            approval=approval,
        )
        return out["id"]

    def write_stdin(self, sandbox: str, container: str, data: Body, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST .../containers/{cid}/stdin: stream bytes (or an iterable of chunks) into stdin.

        The request body is the container's whole stdin: the daemon closes stdin
        when the body ends, so this can be called once per container. To feed
        input over time, pass an iterable (a generator) that yields as it goes.
        A second call raises instead of reporting success for input the process
        would never see.
        """
        key = (sandbox, container)
        with self._stdin_mu:
            if key in self._stdin_sent:
                raise Kosa8Error(
                    f"stdin of container {container} in {sandbox} was already sent and is closed: the daemon "
                    "treats one write_stdin body as the whole of stdin. Pass an iterable to stream input"
                )
            self._stdin_sent.add(key)
        return self.transport.json(
            "POST",
            f"/kosa8/v1/sandboxes/{enc(sandbox)}/containers/{enc(container)}/stdin",
            raw_body=data,
            approval=approval,
        )

    def attach(
        self,
        sandbox: str,
        container: str,
        *,
        on_stdout: Optional[Chunk] = None,
        on_stderr: Optional[Chunk] = None,
        timeout: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> ExecResult:
        """POST .../containers/{cid}/attach: follow a container's output until it exits."""
        frames = self.transport.frames(
            "POST",
            f"/kosa8/v1/sandboxes/{enc(sandbox)}/containers/{enc(container)}/attach",
            approval=approval,
            timeout=timeout,
        )
        return _collect(frames, on_stdout, on_stderr, None)

    # --- snapshots, fork, explore -------------------------------------------------

    def snapshot(
        self, sandbox: str, *, id: Optional[str] = None, encrypt: bool = False, approval: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes/{id}/snapshot: capture memory, processes and disks."""
        body: Dict[str, Any] = {}
        if id:
            body["id"] = id
        if encrypt:
            body["encrypt"] = True
        return self.transport.json(
            "POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/snapshot", body=body, approval=approval
        )

    def list_snapshots(self) -> List[Dict[str, Any]]:
        return self.transport.json("GET", "/kosa8/v1/snapshots") or []

    def restore_snapshot(
        self,
        snapshot_id: str,
        *,
        name: Optional[str] = None,
        label: Optional[str] = None,
        allow_tampered: bool = False,
        approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /kosa8/v1/snapshots/{id}/restore: boot a sandbox from a snapshot, processes resumed."""
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if label:
            body["label"] = label
        if allow_tampered:
            body["allow_tampered"] = True
        return self.transport.json(
            "POST", f"/kosa8/v1/snapshots/{enc(snapshot_id)}/restore", body=body, approval=approval
        )

    def delete_snapshot(
        self, snapshot_id: str, *, wipe: bool = False, force: bool = False, approval: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.transport.json(
            "DELETE",
            f"/kosa8/v1/snapshots/{enc(snapshot_id)}",
            query={"wipe": wipe, "force": force},
            approval=approval,
        )

    def fork(
        self,
        sandbox: str,
        n: int = 1,
        *,
        prefix: Optional[str] = None,
        label: Optional[str] = None,
        approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /kosa8/v1/sandboxes/{id}/fork: N live copies of a running sandbox."""
        body: Dict[str, Any] = {"n": n}
        if prefix:
            body["prefix"] = prefix
        if label:
            body["label"] = label
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/fork", body=body, approval=approval)

    def explore(
        self,
        sandbox: str,
        variants: Sequence[str],
        *,
        image: str = "alpine:latest",
        keep_all: bool = False,
        keep: Optional[str] = None,
        timeout: float = 120.0,
        prefix: Optional[str] = None,
        pull: bool = True,
        approval: Optional[str] = None,
    ) -> ExploreResult:
        """Best-of-N, as ``kosa8 explore``.

        Forks the sandbox once per variant, runs each (``sh -c VARIANT`` in
        ``image``) concurrently in its own branch, keeps the fastest successful
        branch and deletes the rest.
        """
        variants = list(variants)
        if len(variants) < 2:
            raise Kosa8Error("give at least two variants; with one there is nothing to compare")
        if pull:
            self.pull_into_sandbox(sandbox, image, approval=approval)
        prefix = prefix or f"{sandbox}-explore-{int(time.time()) % 100000}"
        forked = self.fork(sandbox, len(variants), prefix=prefix, approval=approval)
        names = [f["id"] for f in forked.get("forks", []) if isinstance(f.get("id"), str)]
        if len(names) < len(variants):
            raise Kosa8Error(
                f"forked {len(names)} branches for {len(variants)} variants; see list_sandboxes() for what was created"
            )
        with ThreadPoolExecutor(max_workers=len(variants)) as pool:
            branches = list(
                pool.map(lambda nv: self._run_branch(nv[0], image, nv[1], timeout, approval), zip(names, variants))
            )
        winner, best = "", float("inf")
        for b in branches:
            if b.error is None and b.exit == 0 and b.duration_ms < best:
                winner, best = b.name, b.duration_ms
        if keep is not None:
            if keep not in names:
                raise Kosa8Error(f"keep {keep} is not one of the branches ({', '.join(names)}); all of them were kept")
            winner = keep
        removed = 0
        if not keep_all:
            for b in branches:
                if b.name != winner:
                    try:
                        self.transport.json("DELETE", f"/kosa8/v1/sandboxes/{enc(b.name)}", approval=approval)
                        removed += 1
                    except Kosa8Error:
                        pass  # reported by the count
        return ExploreResult(parent=sandbox, branches=branches, winner=winner, removed=removed)

    def _run_branch(
        self, name: str, image: str, variant: str, timeout: float, approval: Optional[str]
    ) -> ExploreBranch:
        start = time.monotonic()
        r = ExploreBranch(name=name, variant=variant)
        # A freshly restored branch can refuse the first connection while its
        # engine settles; retry connection-level failures briefly, as the CLI does.
        for attempt in range(4):
            try:
                res = self.run(name, image, ["sh", "-c", variant], timeout=timeout, approval=approval)
                r.exit = res.exit_code
                r.output = (res.stdout + res.stderr).rstrip("\n")
                r.error = None
                break
            except Kosa8Error as err:
                r.error = str(err)
                if isinstance(err, Kosa8StreamError):
                    r.output = (err.stdout + err.stderr).rstrip("\n")
                if isinstance(err, Kosa8TimeoutError) or not _TRANSIENT.search(r.error) or attempt == 3:
                    break
                time.sleep((attempt + 1) * 0.5)
        r.duration_ms = int((time.monotonic() - start) * 1000)
        return r

    # --- checkpoints (client-side over snapshots, as pkg/checkpoint) ---------------

    def checkpoint(self, sandbox: str, *, label: str = "", approval: Optional[str] = None) -> Dict[str, Any]:
        """Save a restore point: a snapshot ``<sandbox>-ckpt-<n>`` on the sandbox's stack.

        The same store ``kosa8 checkpoint`` uses; the oldest is evicted past 10.
        """
        # Two checkpoints taken at once (here or by the CLI) can pick the same
        # next id; the loser's snapshot is refused with 409. Pick again.
        # The winner records its id only once its snapshot is done, so the
        # loser steps past the taken id rather than re-reading the store.
        cid, evict = self.checkpoints.next_id(sandbox)
        for attempt in range(10):
            try:
                snap = self.snapshot(sandbox, id=cid, approval=approval)
                break
            except Kosa8ConflictError as err:
                if attempt == 9 or "already exists" not in str(err.daemon_message or err):
                    raise
                cid = f"{sandbox}-ckpt-{_seq_of(cid, sandbox) + 1}"
        for old in evict:  # evict only once the replacement exists
            try:
                self.delete_snapshot(old, approval=approval)
            except Kosa8Error:
                pass
        rec = self.checkpoints.record(cid, sandbox, label, evict)
        return {"checkpoint": rec, "evicted": evict, "snapshot": snap}

    def list_checkpoints(self, sandbox: str) -> List[Dict[str, Any]]:
        """A sandbox's restore points, oldest first."""
        return self.checkpoints.list(sandbox)

    def rewind(self, sandbox: str, *, to: Optional[str] = None, approval: Optional[str] = None) -> Dict[str, Any]:
        """Undo: put the sandbox back to a checkpoint under the same name, processes resumed.

        Deletes the sandbox, then restores the snapshot with its name;
        checkpoints taken after the target are discarded. Current state is lost.
        """
        target = self.checkpoints.resolve(sandbox, to) if to else self.checkpoints.latest(sandbox)
        # The sandbox must go first: the restored VM takes its name. A refusal
        # here leaves everything as it was, and is raised as is.
        self.transport.json("DELETE", f"/kosa8/v1/sandboxes/{enc(sandbox)}", approval=approval)
        try:
            restored = self.restore_snapshot(target["id"], name=sandbox, approval=approval)
        except Kosa8Error as err:
            raise Kosa8CheckpointError(
                f"restore failed and {sandbox} no longer exists (it was deleted to free its name for the "
                f"restore): {err} — the checkpoint is intact; recover with "
                f"restore_snapshot({target['id']!r}, name={sandbox!r})"
            ) from err
        dropped: List[str] = []
        try:
            dropped = self.checkpoints.pop(sandbox, target["id"])
            for sid in dropped:
                try:
                    self.delete_snapshot(sid, approval=approval)
                except Kosa8Error:
                    pass
        except Kosa8Error:
            pass  # bookkeeping
        return {
            "sandbox": restored.get("sandbox"),
            "checkpoint": target,
            "dropped": dropped,
            "restore_ms": restored.get("restore_ms"),
        }

    # --- helpers --------------------------------------------------------------------

    def sandbox(self, sandbox_id: str) -> "Sandbox":
        """A handle bound to one sandbox."""
        return Sandbox(self, sandbox_id)


def _collect(
    frames: Iterator[Dict[str, Any]],
    on_stdout: Optional[Chunk],
    on_stderr: Optional[Chunk],
    on_status: Optional[Chunk],
) -> ExecResult:
    out: List[bytes] = []
    err: List[bytes] = []
    for f in frames:
        if f.get("done"):
            so, se = b"".join(out), b"".join(err)
            if f.get("error"):
                raise Kosa8StreamError(
                    f["error"], so.decode("utf-8", "replace"), se.decode("utf-8", "replace"), daemon_message=f["error"]
                )
            ex = f.get("exit")
            return ExecResult(exit_code=ex if isinstance(ex, int) else 0, stdout_bytes=so, stderr_bytes=se)
        data = f.get("data")
        if not data:
            continue
        stream = f.get("stream")
        if stream == "stderr":
            err.append(data)
            if on_stderr:
                on_stderr(data)
        elif stream == "status":
            if on_status:
                on_status(data)
        else:
            out.append(data)
            if on_stdout:
                on_stdout(data)
    raise Kosa8StreamError(
        "stream ended unexpectedly (no final frame)",
        b"".join(out).decode("utf-8", "replace"),
        b"".join(err).decode("utf-8", "replace"),
    )


class Sandbox:
    """The client's sandbox methods bound to one sandbox id."""

    def __init__(self, client: Kosa8Client, sandbox_id: str) -> None:
        self.client = client
        self.id = sandbox_id

    def __repr__(self) -> str:
        return f"Sandbox({self.id!r})"

    def info(self) -> Optional[Dict[str, Any]]:
        return self.client.get_sandbox(self.id)

    def start(self, **kw: Any) -> Dict[str, Any]:
        return self.client.start_sandbox(self.id, **kw)

    def stop(self, **kw: Any) -> Dict[str, Any]:
        return self.client.stop_sandbox(self.id, **kw)

    def delete(self, **kw: Any) -> Dict[str, Any]:
        return self.client.delete_sandbox(self.id, **kw)

    def pull(self, ref: str, **kw: Any) -> Dict[str, Any]:
        return self.client.pull_into_sandbox(self.id, ref, **kw)

    def run(self, image: str, args: Sequence[str] = (), **kw: Any) -> ExecResult:
        return self.client.run(self.id, image, args, **kw)

    def start_container(self, image: str, args: Sequence[str] = (), **kw: Any) -> str:
        return self.client.start_container(self.id, image, args, **kw)

    def exec(self, container: str, args: Sequence[str], **kw: Any) -> ExecResult:
        return self.client.exec(self.id, container, args, **kw)

    def containers(self) -> List[Dict[str, Any]]:
        return self.client.sandbox_containers(self.id)

    def images(self) -> List[str]:
        return self.client.sandbox_images(self.id)

    def egress(self) -> Dict[str, Any]:
        return self.client.sandbox_egress(self.id)

    def snapshot(self, **kw: Any) -> Dict[str, Any]:
        return self.client.snapshot(self.id, **kw)

    def fork(self, n: int = 1, **kw: Any) -> Dict[str, Any]:
        return self.client.fork(self.id, n, **kw)

    def explore(self, variants: Sequence[str], **kw: Any) -> ExploreResult:
        return self.client.explore(self.id, variants, **kw)

    def exec_frames(self, container: str, args: Sequence[str], **kw: Any) -> Iterator[Dict[str, Any]]:
        return self.client.exec_frames(self.id, container, args, **kw)

    def start_interactive(self, image: str, args: Sequence[str] = (), **kw: Any) -> str:
        return self.client.start_interactive(self.id, image, args, **kw)

    def write_stdin(self, container: str, data: Any, **kw: Any) -> Dict[str, Any]:
        return self.client.write_stdin(self.id, container, data, **kw)

    def attach(self, container: str, **kw: Any) -> ExecResult:
        return self.client.attach(self.id, container, **kw)

    def checkpoint(self, **kw: Any) -> Dict[str, Any]:
        return self.client.checkpoint(self.id, **kw)

    def checkpoints(self) -> List[Dict[str, Any]]:
        return self.client.list_checkpoints(self.id)

    def rewind(self, **kw: Any) -> Dict[str, Any]:
        return self.client.rewind(self.id, **kw)
