# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 Cloud features beyond a sandbox's basics: background commands,
durable snapshots, previews, secrets, schedules, the code interpreter and
Pipelines. :mod:`kosa8.cloud` wires these onto :class:`~kosa8.Kosa8Cloud` and
:class:`~kosa8.CloudSandbox`. Answers are the API's own dicts (snake_case
fields, times as the API sends them).
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .errors import Kosa8TimeoutError

#: Pipelines run states that are final.
PIPELINE_DONE_STATES = frozenset({"success", "failure", "cancelled", "skipped", "error"})


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


def env_list(env: Optional[Mapping[str, str]]) -> Optional[List[str]]:
    """``{"K": "v"}`` as the exec API's ``["K=v"]``."""
    if not env:
        return None
    return [f"{k}={v}" for k, v in env.items()]


def argv_of(cmd: Union[str, Sequence[str]]) -> List[str]:
    return ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)


def _check_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(f"port must be 1 to 65535, got {port!r}")


class CloudBackgroundExec:
    """A command started with :meth:`CloudSandbox.exec_background`; it keeps
    running after the call returns. ``info`` is its state as of the last read.
    """

    def __init__(self, cloud: Any, sandbox_path: str, info: Dict[str, Any]) -> None:
        self._cloud = cloud
        self._base = sandbox_path
        self.info = info

    def __repr__(self) -> str:
        return f"CloudBackgroundExec(id={self.id!r}, state={self.info.get('state')!r})"

    @property
    def id(self) -> str:
        return self.info["id"]

    def status(self) -> Dict[str, Any]:
        """``state`` (running or exited), ``exit`` and ``output`` so far."""
        self.info = self._cloud._request("GET", f"{self._base}/exec/{_q(self.id)}")["exec"]
        return self.info

    def wait(self, *, timeout: float = 600.0, interval: float = 1.0) -> Dict[str, Any]:
        """Poll until it exits; returns its final status (``exit``, ``output``)."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.status()
            if s.get("state") == "exited":
                return s
            if time.monotonic() + interval > deadline:
                raise Kosa8TimeoutError(f"background command {self.id} still running after {timeout} s")
            time.sleep(interval)


class CloudSnapshots:
    """The org's durable snapshots: memory, processes and disk, kept until
    deleted. Start a sandbox from one with ``cloud.sandboxes.create(snapshot=...)``.
    """

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def create(self, sandbox_id: str, *, name: Optional[str] = None) -> Dict[str, Any]:
        """Snapshot a sandbox by id (same as :meth:`CloudSandbox.snapshot`)."""
        body = {"name": name} if name is not None else {}
        return self._cloud._request("POST", f"/v1/sandboxes/{_q(sandbox_id)}/snapshots", body)["snapshot"]

    def list(self, *, sandbox_id: Optional[str] = None) -> List[Dict[str, Any]]:  # noqa: A003
        """Newest first; with ``sandbox_id`` only that sandbox's."""
        path = "/v1/snapshots" + (f"?sandbox_id={_q(sandbox_id)}" if sandbox_id else "")
        return (self._cloud._request("GET", path) or {}).get("snapshots", [])

    def get(self, snapshot_id: str) -> Dict[str, Any]:
        return self._cloud._request("GET", f"/v1/snapshots/{_q(snapshot_id)}")["snapshot"]

    def delete(self, snapshot_id: str) -> None:
        """Delete it; sandboxes made from it keep running."""
        self._cloud._request("DELETE", f"/v1/snapshots/{_q(snapshot_id)}")


class CloudPreviews:
    """A sandbox's ports at https URLs: ``box.previews.create(3000)["url"]``."""

    def __init__(self, cloud: Any, sandbox_path: str) -> None:
        self._cloud = cloud
        self._base = sandbox_path

    def create(
        self,
        port: int,
        *,
        public: Optional[bool] = None,
        expires_in_s: Optional[int] = None,
        wake: Optional[bool] = None,
        wake_per_hour: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Open a port at its own URL: private (a signed link) unless ``public``.
        ``wake`` wakes a sleeping sandbox on a request."""
        _check_port(port)
        body: Dict[str, Any] = {"port": port}
        for k, v in (("public", public), ("expires_in_s", expires_in_s), ("wake", wake), ("wake_per_hour", wake_per_hour)):
            if v is not None:
                body[k] = v
        return self._cloud._request("POST", f"{self._base}/previews", body)["preview"]

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        """Its previews (no tokens)."""
        return (self._cloud._request("GET", f"{self._base}/previews") or {}).get("previews", [])

    def link(self, port: int, *, expires_in_s: Optional[int] = None) -> Dict[str, Any]:
        """A fresh signed link for a private preview."""
        _check_port(port)
        body = {"expires_in_s": expires_in_s} if expires_in_s is not None else {}
        return self._cloud._request("POST", f"{self._base}/previews/{port}/link", body)["preview"]

    def rotate(self, port: int) -> Dict[str, Any]:
        """A new host name; links to the old one stop working."""
        _check_port(port)
        return self._cloud._request("POST", f"{self._base}/previews/{port}/rotate", {})["preview"]

    def delete(self, port: int) -> None:
        _check_port(port)
        self._cloud._request("DELETE", f"{self._base}/previews/{port}")


class CloudSecrets:
    """The org's secrets, kept encrypted. A sandbox created with
    ``secrets=["NAME"]`` holds only a placeholder, swapped for the value on the
    way out to ``upstream``. A value goes in one request body and is never
    returned, logged or put in a URL.
    """

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def set(  # noqa: A003
        self,
        name: str,
        value: str,
        *,
        upstream: Optional[str] = None,
        header: Optional[str] = None,
        scheme: Optional[str] = None,
        methods: Optional[Sequence[str]] = None,
        paths: Optional[Sequence[str]] = None,
        env: Optional[Mapping[str, str]] = None,
        pipelines: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Store (or replace) a secret. Returns ``{"secret": {...}, "replaced": bool}`` (no value)."""
        if not name:
            raise ValueError("a secret needs a name")
        if not isinstance(value, str) or value == "":
            raise ValueError("a secret needs a value")
        body: Dict[str, Any] = {"value": value}
        for k, v in (
            ("upstream", upstream), ("header", header), ("scheme", scheme),
            ("methods", list(methods) if methods is not None else None),
            ("paths", list(paths) if paths is not None else None),
            ("env", dict(env) if env is not None else None), ("pipelines", pipelines),
        ):
            if v is not None:
                body[k] = v
        r = self._cloud._request("PUT", f"/v1/secrets/{_q(name)}", body) or {}
        return {"secret": r.get("secret", {}), "replaced": r.get("replaced") is True}

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        """Names and settings, never values."""
        return (self._cloud._request("GET", "/v1/secrets") or {}).get("secrets", [])

    def delete(self, name: str) -> Dict[str, Any]:
        """Delete it; sandboxes holding it keep it until deleted (``still_held_by``)."""
        r = self._cloud._request("DELETE", f"/v1/secrets/{_q(name)}") or {}
        return {"deleted": r.get("deleted", name), "still_held_by": r.get("still_held_by", [])}


class CloudSchedules:
    """Commands a sandbox runs on a cron schedule (UTC)."""

    def __init__(self, cloud: Any, sandbox_path: str) -> None:
        self._cloud = cloud
        self._base = sandbox_path

    def create(
        self,
        cron: str,
        cmd: Union[str, Sequence[str]],
        *,
        env: Optional[Mapping[str, str]] = None,
        name: Optional[str] = None,
        wake: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """``cron`` is five fields, UTC (``"0 * * * *"``); a string ``cmd`` runs through ``sh -c``."""
        if not cron:
            raise ValueError('a schedule needs a cron, e.g. "0 * * * *"')
        body: Dict[str, Any] = {"cron": cron, "cmd": argv_of(cmd)}
        el = env_list(env)
        if el:
            body["env"] = el
        if name is not None:
            body["name"] = name
        if wake is not None:
            body["wake"] = wake
        return self._cloud._request("POST", f"{self._base}/schedules", body)["schedule"]

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return (self._cloud._request("GET", f"{self._base}/schedules") or {}).get("schedules", [])

    def run(self, schedule_id: str) -> Dict[str, Any]:
        """Run it now; returns ``{"schedule": ..., "run": {status, exit, output, ...}}``."""
        return self._cloud._request("POST", f"{self._base}/schedules/{_q(schedule_id)}/run", {})

    def delete(self, schedule_id: str) -> None:
        self._cloud._request("DELETE", f"{self._base}/schedules/{_q(schedule_id)}")


class CloudCodeContexts:
    """A sandbox's code-interpreter contexts; each keeps its own globals."""

    def __init__(self, cloud: Any, sandbox_path: str) -> None:
        self._cloud = cloud
        self._base = sandbox_path

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return (self._cloud._request("GET", f"{self._base}/code/contexts") or {}).get("contexts", [])

    def create(self, name: Optional[str] = None) -> Dict[str, Any]:
        body = {"name": name} if name is not None else {}
        return self._cloud._request("POST", f"{self._base}/code/contexts", body)["context"]

    def reset(self, name: str) -> None:
        """Clear its globals."""
        self._cloud._request("POST", f"{self._base}/code/contexts/{_q(name)}/reset", {})

    def delete(self, name: str) -> None:
        self._cloud._request("DELETE", f"{self._base}/code/contexts/{_q(name)}")


_PIPELINE_FIELDS = (
    "ref", "event", "workflow", "job", "inputs", "size", "secrets", "token_secret", "status_secret",
    "timeout_s", "provider", "name", "secret", "cron", "cron_ref",
)


def _pipeline_body(repo_url: str, fields: Mapping[str, Any]) -> Dict[str, Any]:
    if not repo_url:
        raise ValueError("a pipeline needs repo_url")
    body: Dict[str, Any] = {"repo_url": repo_url}
    for k in _PIPELINE_FIELDS:
        v = fields.get(k)
        if v is not None:
            body[k] = list(v) if k == "secrets" else v
    return body


def pipeline_run_done(run: Mapping[str, Any]) -> bool:
    """Has a run finished?"""
    return run.get("state") in PIPELINE_DONE_STATES or bool(run.get("finished_at"))


class CloudPipelines:
    """CI on kosa8 Cloud: runs of a repository's ``.kosa8/pipeline.yml`` (or
    its GitHub workflows through act), their logs and artifacts, and
    webhook/cron triggers (:attr:`hooks`)."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud
        self.hooks = CloudPipelineHooks(cloud)

    def run(
        self,
        repo_url: str,
        *,
        ref: Optional[str] = None,
        event: Optional[str] = None,
        workflow: Optional[str] = None,
        job: Optional[str] = None,
        inputs: Optional[Mapping[str, str]] = None,
        size: Optional[str] = None,
        secrets: Optional[Sequence[str]] = None,
        token_secret: Optional[str] = None,
        status_secret: Optional[str] = None,
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start a run. ``secrets`` are names of org secrets stored with
        ``pipelines=True``; ``token_secret`` holds a token to clone a private
        repository and ``status_secret`` one to post commit statuses."""
        body = _pipeline_body(repo_url, dict(
            ref=ref, event=event, workflow=workflow, job=job, inputs=dict(inputs) if inputs else None, size=size,
            secrets=secrets, token_secret=token_secret, status_secret=status_secret, timeout_s=timeout_s,
        ))
        return self._cloud._request("POST", "/v1/pipelines/runs", body)["run"]

    def list(self, *, state: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:  # noqa: A003
        """Runs, newest first."""
        q: Dict[str, str] = {}
        if state:
            q["state"] = state
        if limit is not None:
            q["limit"] = str(limit)
        path = "/v1/pipelines/runs" + ("?" + urllib.parse.urlencode(q) if q else "")
        return (self._cloud._request("GET", path) or {}).get("runs", [])

    def get(self, run_id: str) -> Dict[str, Any]:
        return self._cloud._request("GET", f"/v1/pipelines/runs/{_q(run_id)}")["run"]

    def logs(self, run_id: str, *, job: Optional[str] = None, after: Optional[int] = None) -> Dict[str, Any]:
        """Logs by job, secrets replaced with ``***``. ``after`` is a byte
        offset: pass a previous answer's ``logs[i]["next"]`` to follow."""
        q: Dict[str, str] = {}
        if job:
            q["job"] = job
        if after is not None:
            q["after"] = str(after)
        path = f"/v1/pipelines/runs/{_q(run_id)}/logs" + ("?" + urllib.parse.urlencode(q) if q else "")
        return self._cloud._request("GET", path)

    def cancel(self, run_id: str) -> Dict[str, Any]:
        return self._cloud._request("POST", f"/v1/pipelines/runs/{_q(run_id)}/cancel", {})["run"]

    def artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        return (self._cloud._request("GET", f"/v1/pipelines/runs/{_q(run_id)}/artifacts") or {}).get("artifacts", [])

    def download_artifact(self, run_id: str, job: str, idx: int) -> bytes:
        """An artifact's bytes (a .tar.gz)."""
        return self._cloud._request("GET", f"/v1/pipelines/runs/{_q(run_id)}/artifacts/{_q(job)}/{int(idx)}", raw=True)

    def wait(self, run_id: str, *, timeout: float = 3600.0, interval: float = 3.0) -> Dict[str, Any]:
        """Poll until the run finishes; returns it."""
        deadline = time.monotonic() + timeout
        while True:
            run = self.get(run_id)
            if pipeline_run_done(run):
                return run
            if time.monotonic() + interval > deadline:
                raise Kosa8TimeoutError(f"pipeline run {run_id} not finished after {timeout} s")
            time.sleep(interval)


class CloudPipelineHooks:
    """Webhook (push / pull request) and cron triggers for Pipelines."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def create(
        self,
        repo_url: str,
        *,
        provider: Optional[str] = None,
        name: Optional[str] = None,
        secret: Optional[str] = None,
        size: Optional[str] = None,
        secrets: Optional[Sequence[str]] = None,
        token_secret: Optional[str] = None,
        status_secret: Optional[str] = None,
        workflow: Optional[str] = None,
        timeout_s: Optional[int] = None,
        cron: Optional[str] = None,
        cron_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make a webhook URL for a repository: give its ``url`` and
        ``secret`` to the git host. ``cron`` also runs it on a schedule."""
        body = _pipeline_body(repo_url, dict(
            provider=provider, name=name, secret=secret, size=size, secrets=secrets, token_secret=token_secret,
            status_secret=status_secret, workflow=workflow, timeout_s=timeout_s, cron=cron, cron_ref=cron_ref,
        ))
        return self._cloud._request("POST", "/v1/pipelines/hooks", body)["hook"]

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return (self._cloud._request("GET", "/v1/pipelines/hooks") or {}).get("hooks", [])

    def delete(self, hook_id: str) -> None:
        self._cloud._request("DELETE", f"/v1/pipelines/hooks/{_q(hook_id)}")


__all__ = [
    "CloudBackgroundExec",
    "CloudSnapshots",
    "CloudPreviews",
    "CloudSecrets",
    "CloudSchedules",
    "CloudCodeContexts",
    "CloudPipelines",
    "CloudPipelineHooks",
    "pipeline_run_done",
    "PIPELINE_DONE_STATES",
]


class CloudTemplates:
    """The org's own templates on kosa8 Cloud, built once and restored in about a
    second instead of pulling the image on every create (an uncached pull of an
    RL task image takes ~17 s).

    ``build`` takes an image reference (a tag, or ``name@sha256:…`` to pin it)
    or a Dockerfile's text; ``import_images`` queues many images at once (say,
    a SWE-bench split by digest). A sandbox is then
    ``cloud.sandboxes.create(template=name)``. Reached as ``cloud.org_templates``.
    """

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    @staticmethod
    def _path(ref: str) -> str:
        return "/v1/templates/" + urllib.parse.quote(ref, safe="")

    def build(
        self,
        name: str,
        *,
        image: Optional[str] = None,
        dockerfile: Optional[str] = None,
        cpus: Optional[int] = None,
        mem_mb: Optional[int] = None,
        build_args: Optional[Mapping[str, str]] = None,
        setup_cmd: Optional[Sequence[str]] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a build; returns the template (state ``building``). ``wait`` it."""
        if (image is None) == (dockerfile is None):
            raise ValueError("give image or dockerfile, one of them")
        body: Dict[str, Any] = {"name": name}
        for k, v in (("image", image), ("dockerfile", dockerfile), ("cpus", cpus), ("mem_mb", mem_mb),
                     ("setup_cmd", list(setup_cmd) if setup_cmd is not None else None), ("region", region)):
            if v is not None:
                body[k] = v
        if build_args:
            body["build_args"] = [f"{k}={v}" for k, v in build_args.items()]
        return (self._cloud._request("POST", "/v1/templates", body) or {}).get("template", {})

    def import_images(
        self,
        images: Union[Mapping[str, str], Sequence[Mapping[str, str]]],
        *,
        cpus: Optional[int] = None,
        mem_mb: Optional[int] = None,
        setup_cmd: Optional[Sequence[str]] = None,
        partial_ok: bool = True,
    ) -> Dict[str, Any]:
        """Queue one build per image: ``{name: image}`` or ``[{"name", "image"}]``.
        Returns ``{import_id, queued, templates, failed}``."""
        items = [{"name": k, "image": v} for k, v in images.items()] if isinstance(images, Mapping) else [dict(i) for i in images]
        body: Dict[str, Any] = {"images": items, "partial_ok": partial_ok}
        for k, v in (("cpus", cpus), ("mem_mb", mem_mb), ("setup_cmd", list(setup_cmd) if setup_cmd is not None else None)):
            if v is not None:
                body[k] = v
        return self._cloud._request("POST", "/v1/templates/import", body) or {}

    def get(self, ref: str) -> Dict[str, Any]:
        """One template by id or name, with its build log."""
        return (self._cloud._request("GET", self._path(ref)) or {}).get("template", {})

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        """The org's own templates (``org_templates`` of ``GET /v1/templates``)."""
        return (self._cloud._request("GET", "/v1/templates") or {}).get("org_templates", [])

    def delete(self, ref: str) -> None:
        self._cloud._request("DELETE", self._path(ref))

    def wait(self, ref: str, *, timeout: float = 1800.0, interval: float = 5.0) -> Dict[str, Any]:
        """Poll until the template is ready; raise on ``failed`` (with the log's
        tail) or past ``timeout``."""
        end = time.monotonic() + timeout
        while True:
            t = self.get(ref)
            if t.get("state") == "ready":
                return t
            if t.get("state") == "failed":
                raise TemplateBuildError(ref, t)
            if time.monotonic() >= end:
                raise Kosa8TimeoutError(f"template {ref} not ready after {timeout} s (state {t.get('state')})")
            time.sleep(interval)


class TemplateBuildError(Exception):
    """A template build failed; ``template`` holds its record and ``log``."""

    def __init__(self, ref: str, template: Mapping[str, Any]) -> None:
        self.template = dict(template)
        tail = str(template.get("log") or "")[-1500:]
        super().__init__(f"template {ref} failed to build:\n{tail}")
