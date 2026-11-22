# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Hosted jobs on kosa8 Cloud: send tasks, a solver and a grader, and kosa8
runs every rollout server-side and records it as an RL run.

>>> import kosa8
>>> cloud = kosa8.Cloud()
>>> job = cloud.jobs.submit(
...     name="swe-eval", template="python", tasks="tasks.jsonl",
...     solver={"cmd": "python /workspace/solve.py", "timeout_s": 1800},
...     grader={"cmd": "pytest -q /hidden/test.py", "files": {"/hidden/test.py": open("test.py").read()}},
...     concurrency=64, budget_usd=20)
>>> final = cloud.jobs.wait(job["id"], progress=lambda j: print(j["progress"]))

The job's rollouts land in the RL run ``job["run_id"]`` (step 0, group = the
task id, reward from the sealed grader): :class:`kosa8.rl.Run` reads them.
Reached as ``cloud.jobs`` (and ``await cloud.jobs...`` on :class:`~kosa8.AsyncCloud`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from .errors import Kosa8TimeoutError

#: Job statuses after which nothing more runs.
JOB_DONE_STATES = frozenset({"done", "failed", "cancelled", "budget_reached"})
#: The most tasks one job takes.
MAX_TASKS = 10_000

Cmd = Union[str, Sequence[str]]
FileValue = Union[bytes, bytearray, str, "os.PathLike[str]"]
Tasks = Union[str, "os.PathLike[str]", Iterable[Mapping[str, Any]]]


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


def job_done(job: Mapping[str, Any]) -> bool:
    """Whether a job (as the API answers it) has finished."""
    return job.get("status") in JOB_DONE_STATES


def _b64(v: FileValue) -> str:
    """A file's contents as base64: bytes as they are, a ``Path`` (or
    ``os.PathLike``) read from disk, a ``str`` as UTF-8 text."""
    if isinstance(v, (bytes, bytearray)):
        data = bytes(v)
    elif isinstance(v, os.PathLike):
        with open(v, "rb") as f:
            data = f.read()
    elif isinstance(v, str):
        data = v.encode()
    else:
        raise TypeError(f"a file is bytes, str or a Path, got {type(v).__name__}")
    return base64.b64encode(data).decode()


def encode_files(files: Optional[Mapping[str, FileValue]]) -> Optional[Dict[str, str]]:
    """``{"/abs/path": bytes | str | Path}`` as the API's ``{"/abs/path": base64}``."""
    if files is None:
        return None
    out: Dict[str, str] = {}
    for p, v in files.items():
        if not isinstance(p, str) or not p.startswith("/"):
            raise ValueError(f"file paths are absolute (in the sandbox), got {p!r}")
        out[p] = _b64(v)
    return out


def load_tasks(tasks: Tasks) -> List[Dict[str, Any]]:
    """Tasks as the API takes them: a list of ``{"id", "input"?, "files"?}``
    dicts, or a path to a ``.jsonl`` file of them (one per line, blank lines
    skipped). A task's ``files`` values may be bytes, str or a Path; they are
    sent base64-encoded. Task ids must be unique."""
    if isinstance(tasks, (str, os.PathLike)):
        rows: List[Any] = []
        with open(tasks, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError as e:
                    raise ValueError(f"{os.fspath(tasks)}:{n}: not JSON: {e}") from None
    else:
        rows = list(tasks)
    if not 1 <= len(rows) <= MAX_TASKS:
        raise ValueError(f"a job has 1 to {MAX_TASKS} tasks, got {len(rows)}")
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, t in enumerate(rows):
        if not isinstance(t, Mapping):
            raise ValueError(f"tasks[{i}] must be an object with an id")
        tid = t.get("id")
        if isinstance(tid, int) and not isinstance(tid, bool):
            tid = str(tid)
        if not isinstance(tid, str) or not tid:
            raise ValueError(f"tasks[{i}] needs an id (a string)")
        if tid in seen:
            raise ValueError(f"task id {tid!r} appears twice")
        seen.add(tid)
        row: Dict[str, Any] = {"id": tid}
        if t.get("input") is not None:
            row["input"] = t["input"]
        if t.get("files"):
            files = t["files"]
            # Already base64 from a .jsonl: kept as is.
            row["files"] = dict(files) if isinstance(tasks, (str, os.PathLike)) else encode_files(files)
        out.append(row)
    return out


def _cmd_spec(spec: Union[Cmd, Mapping[str, Any]], what: str, *, files: bool) -> Dict[str, Any]:
    if isinstance(spec, (str, list, tuple)):
        spec = {"cmd": spec}
    if not isinstance(spec, Mapping) or not spec.get("cmd"):
        raise ValueError(f"{what} needs cmd (a string run with sh -c, or a list of arguments)")
    out: Dict[str, Any] = {"cmd": spec["cmd"] if isinstance(spec["cmd"], str) else list(spec["cmd"])}
    for k in ("timeout_s",):
        if spec.get(k) is not None:
            out[k] = spec[k]
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    if files and spec.get("files"):
        out["files"] = encode_files(spec["files"])
    extra = set(spec) - {"cmd", "timeout_s", "env", "files"}
    if extra or (not files and "files" in spec):
        raise ValueError(f"{what}: unknown fields {sorted(extra | ({'files'} if not files and 'files' in spec else set()))}")
    return out


def job_body(
    *,
    name: str,
    tasks: Tasks,
    solver: Union[Cmd, Mapping[str, Any]],
    grader: Union[Cmd, Mapping[str, Any]],
    template: Optional[str] = None,
    image: Optional[str] = None,
    snapshot: Optional[str] = None,
    size: Optional[str] = None,
    project: Optional[str] = None,
    solver_timeout_s: Optional[int] = None,
    grader_timeout_s: Optional[int] = None,
    grader_files: Optional[Mapping[str, FileValue]] = None,
    concurrency: Optional[int] = None,
    retries_on_infra: Optional[int] = None,
    lease_s: Optional[int] = None,
    budget_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """The body of ``POST /v1/jobs`` (and ``/v1/jobs/estimate``)."""
    if not name:
        raise ValueError("a job needs a name")
    if sum(x is not None for x in (template, image, snapshot)) != 1:
        raise ValueError("give exactly one of template, image or snapshot")
    s = _cmd_spec(solver, "solver", files=False)
    if solver_timeout_s is not None:
        s["timeout_s"] = solver_timeout_s
    if "timeout_s" not in s:
        raise ValueError("solver needs timeout_s (seconds; a solver past it is recorded as timeout)")
    g = _cmd_spec(grader, "grader", files=True)
    if grader_timeout_s is not None:
        g["timeout_s"] = grader_timeout_s
    if grader_files:
        g["files"] = {**g.get("files", {}), **(encode_files(grader_files) or {})}
    body: Dict[str, Any] = {"name": name, "tasks": load_tasks(tasks), "solver": s, "grader": g}
    for k, v in (("project", project), ("template", template), ("image", image), ("snapshot", snapshot),
                 ("size", size), ("concurrency", concurrency), ("retries_on_infra", retries_on_infra),
                 ("lease_s", lease_s), ("budget_usd", budget_usd)):
        if v is not None:
            body[k] = v
    return body


def _list_path(status: Optional[str], project: Optional[str], limit: Optional[int], cursor: Optional[str]) -> str:
    q = {k: str(v) for k, v in (("status", status), ("project", project), ("limit", limit), ("cursor", cursor))
         if v is not None}
    return "/v1/jobs" + ("?" + urllib.parse.urlencode(q) if q else "")


def _estimate_body(kw: Dict[str, Any], sample: Optional[int], sample_job_id: Optional[str]) -> Dict[str, Any]:
    body = job_body(**kw)
    if sample is not None and sample_job_id is not None:
        raise ValueError("give sample or sample_job_id, not both")
    if sample is not None:
        body["sample"] = sample
    if sample_job_id is not None:
        body["sample_job_id"] = sample_job_id
    return body


class CloudJobs:
    """Hosted jobs (``cloud.jobs``): kosa8 runs every task's rollout itself.

    Each task gets its own sandbox (from ``template``, ``image`` or
    ``snapshot``); the solver runs there with the task's input in
    ``$KOSA8_TASK_INPUT`` and a file, then the sealed grader scores a copy
    with no network and the hidden ``grader`` files. Infra failures are
    retried ``retries_on_infra`` times, then recorded masked (reward None).
    """

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def submit(self, *, idempotency_key: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        """Start a job; returns it (``id``, ``run_id``, ``status``, ...).

        Keyword arguments are those of :func:`job_body`: ``name``, ``tasks``
        (a list of dicts or a ``.jsonl`` path), ``solver`` and ``grader`` (a
        command, or ``{"cmd", "timeout_s", "env", "files"}``), one of
        ``template`` / ``image`` / ``snapshot``, and ``concurrency``,
        ``retries_on_infra``, ``lease_s``, ``budget_usd``. The call carries an
        Idempotency-Key (``idempotency_key``, or a new one), so a retry after
        a dropped connection starts the job once."""
        r = self._cloud._request("POST", "/v1/jobs", job_body(**kw), idempotency_key=idempotency_key) or {}
        return _job_of(r)

    def get(self, job_id: str) -> Dict[str, Any]:
        """The job now: ``status``, ``counts``, ``by_failure_class``, ``progress``, ``cost_usd``, ``eta_s``."""
        return _job_of(self._cloud._request("GET", f"/v1/jobs/{_q(job_id)}"))

    def list(self, *, status: Optional[str] = None, project: Optional[str] = None,  # noqa: A003
             limit: Optional[int] = None, cursor: Optional[str] = None) -> List[Dict[str, Any]]:
        """The org's jobs, newest first."""
        return list((self._cloud._request("GET", _list_path(status, project, limit, cursor)) or {}).get("jobs", []))

    def cancel(self, job_id: str) -> Dict[str, Any]:
        """Stop starting rollouts and delete the job's sandboxes; returns the job."""
        return _job_of(self._cloud._request("POST", f"/v1/jobs/{_q(job_id)}/cancel", {}))

    def estimate(self, *, sample: Optional[int] = None, sample_job_id: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        """What a job would use: ``sandbox_hours`` and ``usd_list`` as
        ``{"low", "high"}`` ranges and ``included_hours_left`` on your plan.
        Takes :meth:`submit`'s arguments. Without a sample the range comes from
        the solver's timeout; ``sample=n`` starts a pilot job of the first n
        tasks (its id is in ``["sample"]["job_id"]``), and once it is done
        ``sample_job_id=`` estimates from its measured durations."""
        return self._cloud._request("POST", "/v1/jobs/estimate", _estimate_body(kw, sample, sample_job_id)) or {}

    def wait(self, job_id: str, *, progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
             poll_s: float = 5.0, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Poll until the job finishes (done, failed, cancelled or
        budget_reached); ``progress(job)`` is called on every poll. Returns
        the final job. ``timeout`` (seconds) raises Kosa8TimeoutError; the job
        keeps running on kosa8."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if progress is not None:
                progress(job)
            if job_done(job):
                return job
            if deadline is not None and time.monotonic() + poll_s > deadline:
                raise Kosa8TimeoutError(f"job {job_id} not finished after {timeout} s (status {job.get('status')})")
            time.sleep(poll_s)


class AsyncCloudJobs:
    """:class:`CloudJobs` for :class:`~kosa8.AsyncCloud`: every call is a coroutine."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    async def submit(self, *, idempotency_key: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        return _job_of(await self._cloud._request("POST", "/v1/jobs", job_body(**kw), idempotency_key=idempotency_key))

    async def get(self, job_id: str) -> Dict[str, Any]:
        return _job_of(await self._cloud._request("GET", f"/v1/jobs/{_q(job_id)}"))

    async def list(self, *, status: Optional[str] = None, project: Optional[str] = None,  # noqa: A003
                   limit: Optional[int] = None, cursor: Optional[str] = None) -> List[Dict[str, Any]]:
        return list((await self._cloud._request("GET", _list_path(status, project, limit, cursor)) or {}).get("jobs", []))

    async def cancel(self, job_id: str) -> Dict[str, Any]:
        return _job_of(await self._cloud._request("POST", f"/v1/jobs/{_q(job_id)}/cancel", {}))

    async def estimate(self, *, sample: Optional[int] = None, sample_job_id: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        return await self._cloud._request("POST", "/v1/jobs/estimate", _estimate_body(kw, sample, sample_job_id)) or {}

    async def wait(self, job_id: str, *, progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
                   poll_s: float = 5.0, timeout: Optional[float] = None) -> Dict[str, Any]:
        """See :meth:`CloudJobs.wait`; ``progress`` may be a coroutine function."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = await self.get(job_id)
            if progress is not None:
                r = progress(job)
                if asyncio.iscoroutine(r):
                    await r
            if job_done(job):
                return job
            if deadline is not None and time.monotonic() + poll_s > deadline:
                raise Kosa8TimeoutError(f"job {job_id} not finished after {timeout} s (status {job.get('status')})")
            await asyncio.sleep(poll_s)


def _job_of(answer: Any) -> Dict[str, Any]:
    """The job of an answer: ``{"job": {...}}`` (with ``job_id`` / ``run_id``
    on a submit) as one dict that always has ``id`` and ``run_id``."""
    if not isinstance(answer, dict):
        return {}
    job = dict(answer["job"]) if isinstance(answer.get("job"), dict) else {}
    if answer.get("job_id") and not job.get("id"):
        job["id"] = answer["job_id"]
    if answer.get("run_id") and not job.get("run_id"):
        job["run_id"] = answer["run_id"]
    return job


__all__ = ["CloudJobs", "AsyncCloudJobs", "JOB_DONE_STATES", "job_body", "job_done", "load_tasks", "encode_files"]
