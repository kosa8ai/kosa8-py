# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 Cloud account and platform calls: pricing, limits, usage insights,
images, lineage and checkpoints (:class:`CloudAccount`), API keys, webhooks,
the audit log, ready-made benchmarks, RL runs with compare and alerts, and
functions. :mod:`kosa8.cloud` wires these onto :class:`~kosa8.Kosa8Cloud`
(``cloud.api_keys``, ``cloud.webhooks``, ``cloud.audit``,
``cloud.benchmarks``, ``cloud.rl``, ``cloud.functions``, and
``cloud.pricing()``, ``cloud.limits()``, ...) and :mod:`kosa8.aio` onto
:class:`~kosa8.AsyncCloud`, where every call is a coroutine. Answers are the
API's own dicts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from ._calls import AsyncCalls, Calls, as_dict, key, q, query
from ._calls import query as _query


def _whole(name: str, v: Any, lo: int, hi: Optional[int] = None) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v < lo or (hi is not None and v > hi):
        rng = f"from {lo} to {hi}" if hi is not None else f"of at least {lo}"
        raise ValueError(f"{name} must be a whole number {rng}, got {v!r}")


def _need(name: str, v: Any) -> None:
    if not isinstance(v, str) or not v:
        raise ValueError(f"{name} is required")


# ---------------------------------------------------------------------------
# The account: prices, limits, usage, images, lineage, checkpoints


class CloudAccount(Calls):
    """Read-mostly calls about the org (``cloud.account``; the same methods
    are on the client itself: ``cloud.pricing()``, ``cloud.limits()``...)."""

    def pricing(self) -> Dict[str, Any]:
        """The rate card (``GET /v1/pricing``, public): ``rates`` (``vcpu_h``,
        ``gb_h``, ``browser_h``, ...), ``sizes``, ``plans``..."""
        return self._call("GET", "/v1/pricing", then=as_dict)

    def limits(self) -> Dict[str, Any]:
        """The running-sandbox limit: ``active``, ``max_running`` and the last request to raise it."""
        return self._call("GET", "/v1/limits", then=as_dict)

    def request_limit(self, max_running: int, *, reason: Optional[str] = None) -> Dict[str, Any]:
        """Ask kosa8 for a higher running-sandbox limit (answered by email to the owner)."""
        _whole("max_running", max_running, 1)
        body: Dict[str, Any] = {"max_running": max_running}
        if reason is not None:
            if not isinstance(reason, str) or len(reason) > 500:
                raise ValueError("reason is text of at most 500 characters")
            body["reason"] = reason
        return self._call("POST", "/v1/limits/request", body, then=as_dict)

    def usage(self) -> Dict[str, Any]:
        """This month's usage, bill estimate and whether work is paused for payment."""
        return self._call("GET", "/v1/usage", then=as_dict)

    def usage_insights(self) -> Dict[str, Any]:
        """Daily spend, top sandboxes, credit left and projected month-end spend."""
        return self._call("GET", "/v1/usage/insights", then=as_dict)

    def images(self) -> List[Dict[str, Any]]:
        """Images behind the templates the org's hosts offer, with tag and digest."""
        return self._call("GET", "/v1/images", then=key("images", list))

    def lineage(self) -> Dict[str, Any]:
        """Sandboxes with their fork parents: ``nodes``, ``roots``, ``forked``."""
        return self._call("GET", "/v1/lineage", then=as_dict)

    def checkpoints(self) -> Dict[str, Any]:
        """Checkpoints of the org's running and sleeping sandboxes (up to 25), as
        each host reports them: ``sandboxes`` (each with ``checkpoints``, or
        ``None`` and why)."""
        return self._call("GET", "/v1/checkpoints", then=as_dict)


class AsyncCloudAccount(AsyncCalls, CloudAccount):
    """:class:`CloudAccount` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# API keys


class CloudApiKeys(Calls):
    """The org's API keys (``cloud.api_keys``; needs the ``keys:manage`` scope).
    A secret is in the answer of ``create`` and ``rotate`` only, once."""

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        """Keys, newest first (never secrets)."""
        return self._call("GET", "/v1/api-keys", then=key("api_keys", list))

    def create(self, name: str, *, scopes: Optional[Sequence[str]] = None,
               expires_in_s: Optional[int] = None) -> Dict[str, Any]:
        """A new key: ``{"api_key": {id, name, scopes, ..., secret}, "note"}``. Store the secret now."""
        _need("name", name)
        body: Dict[str, Any] = {"name": name}
        if scopes is not None:
            body["scopes"] = [scopes] if isinstance(scopes, str) else list(scopes)
        if expires_in_s is not None:
            _whole("expires_in_s", expires_in_s, 1)
            body["expires_in_s"] = expires_in_s
        return self._call("POST", "/v1/api-keys", body, then=as_dict)

    def revoke(self, key_id: str) -> Dict[str, Any]:
        """Revoke a key now."""
        _need("key_id", key_id)
        return self._call("DELETE", f"/v1/api-keys/{q(key_id)}", then=as_dict)

    delete = revoke

    def rotate(self, key_id: str, *, grace_s: Optional[int] = None) -> Dict[str, Any]:
        """A new key with the same name and scopes; the old one works ``grace_s``
        more seconds. ``{"api_key": {..., secret}, "old_key": {id, expires_at}}``."""
        _need("key_id", key_id)
        body: Dict[str, Any] = {}
        if grace_s is not None:
            _whole("grace_s", grace_s, 0)
            body["grace_s"] = grace_s
        return self._call("POST", f"/v1/api-keys/{q(key_id)}/rotate", body, then=as_dict)


class AsyncCloudApiKeys(AsyncCalls, CloudApiKeys):
    """:class:`CloudApiKeys` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# Webhooks


class CloudWebhooks(Calls):
    """Webhook endpoints the org's events are sent to (``cloud.webhooks``).
    Each delivery is signed with the endpoint's ``whsec_`` secret, shown once."""

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return self._call("GET", "/v1/webhooks", then=key("webhooks", list))

    def events(self) -> List[str]:
        """The event names an endpoint can subscribe to."""
        return self._call("GET", "/v1/webhooks", then=key("events", list))

    def create(self, url: str, *, events: Optional[Sequence[str]] = None,
               description: Optional[str] = None) -> Dict[str, Any]:
        """Add an endpoint (https). Returns the webhook with its ``secret`` (once)."""
        _need("url", url)
        body: Dict[str, Any] = {"url": url}
        if events is not None:
            body["events"] = [events] if isinstance(events, str) else list(events)
        if description is not None:
            body["description"] = description
        return self._call("POST", "/v1/webhooks", body, then=key("webhook", dict))

    def delete(self, webhook_id: str) -> Dict[str, Any]:
        """Stop sending to it, retries included."""
        _need("webhook_id", webhook_id)
        return self._call("DELETE", f"/v1/webhooks/{q(webhook_id)}", then=as_dict)

    def deliveries(self, webhook_id: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Recent deliveries, newest first."""
        _need("webhook_id", webhook_id)
        if limit is not None:
            _whole("limit", limit, 1, 200)
        return self._call("GET", f"/v1/webhooks/{q(webhook_id)}/deliveries" + query({"limit": limit}),
                          then=key("deliveries", list))

    def test(self, webhook_id: str) -> Dict[str, Any]:
        """Send a ``webhook.test`` event now; returns the delivery."""
        _need("webhook_id", webhook_id)
        return self._call("POST", f"/v1/webhooks/{q(webhook_id)}/test", {}, then=key("delivery", dict))


class AsyncCloudWebhooks(AsyncCalls, CloudWebhooks):
    """:class:`CloudWebhooks` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# Audit log


def _ndjson(data: Any) -> List[Dict[str, Any]]:
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data or "")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class CloudAudit(Calls):
    """The org's hash-chained audit log (``cloud.audit``)."""

    def list(self, *, sandbox_id: Optional[str] = None, actor: Optional[str] = None, action: Optional[str] = None,  # noqa: A003
             since: Optional[Union[int, str]] = None, until: Optional[Union[int, str]] = None,
             before_seq: Optional[int] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """One page, newest first: ``{"events": [...], "next_before_seq"}``;
        pass ``next_before_seq`` as ``before_seq`` for the next page."""
        if limit is not None:
            _whole("limit", limit, 1)
        return self._call("GET", "/v1/audit" + query({
            "sandbox_id": sandbox_id, "actor": actor, "action": action, "since": since, "until": until,
            "before_seq": before_seq, "limit": limit,
        }), then=as_dict)

    def verify(self) -> Dict[str, Any]:
        """Recompute the hash chain: ``ok``, ``count``, ``head`` or ``first_broken_seq`` and ``reason``."""
        return self._call("GET", "/v1/audit/verify", then=as_dict)

    def export(self) -> List[Dict[str, Any]]:
        """Every event, oldest first, with its hashes (the NDJSON export, parsed)."""
        return self._call("GET", "/v1/audit/export", raw=True, then=_ndjson)

    def export_raw(self) -> bytes:
        """The NDJSON export as sent, to keep as evidence."""
        return self._call("GET", "/v1/audit/export", raw=True)


class AsyncCloudAudit(AsyncCalls, CloudAudit):
    """:class:`CloudAudit` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# Benchmarks


class CloudBenchmarks(Calls):
    """Ready-made benchmarks (``cloud.benchmarks``): each instance is a
    template ``"<benchmark>/<instance>"`` to create a sandbox from, or a job's
    ``benchmark``. A create before its environment is built answers 409
    ``template_building`` with a ``retry_after_s``, which the client waits out
    and retries by itself."""

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return self._call("GET", "/v1/benchmarks", then=key("benchmarks", list))

    def get(self, name: str) -> Dict[str, Any]:
        _need("name", name)
        return self._call("GET", f"/v1/benchmarks/{q(name)}", then=key("benchmark", dict))

    def instances(self, name: str, *, state: Optional[str] = None, limit: Optional[int] = None,
                  cursor: Optional[str] = None) -> Dict[str, Any]:
        """One page of instances in the dataset's order: ``{"instances": [...], "next_cursor"}``."""
        _need("name", name)
        if limit is not None:
            _whole("limit", limit, 1)
        return self._call("GET", f"/v1/benchmarks/{q(name)}/instances" + query(
            {"state": state, "limit": limit, "cursor": cursor}), then=as_dict)

    def instance(self, name: str, instance_id: str) -> Dict[str, Any]:
        """One instance: the solver's input and its environment's state and ETA."""
        _need("name", name)
        _need("instance_id", instance_id)
        return self._call("GET", f"/v1/benchmarks/{q(name)}/instances/{q(instance_id)}", then=key("instance", dict))

    def prepare(self, name: str, instances: Union[str, int, Sequence[str]] = "all", *,
                seed: Optional[int] = None) -> Dict[str, Any]:
        """Build environments ahead of use (at most 100 a call): ids, ``"all"``
        or a number of instances (sampled with ``seed``)."""
        _need("name", name)
        inst: Any = instances if isinstance(instances, (str, int)) else list(instances)
        body: Dict[str, Any] = {"instances": inst}
        if seed is not None:
            body["seed"] = seed
        return self._call("POST", f"/v1/benchmarks/{q(name)}/prepare", body, then=as_dict)


class AsyncCloudBenchmarks(AsyncCalls, CloudBenchmarks):
    """:class:`CloudBenchmarks` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# RL runs, compare and alerts

class CloudRL(Calls):
    """RL runs as the API keeps them (``cloud.rl``). :class:`kosa8.rl.Run`
    records rollouts into a run; these read and manage runs, compare them and
    set alert rules on them."""

    def runs(self, *, project: Optional[str] = None, status: Optional[str] = None, name: Optional[str] = None,
             limit: Optional[int] = None, cursor: Optional[str] = None) -> Dict[str, Any]:
        """One page, newest first: ``{"runs": [...], "next_cursor"}``."""
        return self._call("GET", "/v1/rl/runs" + query(
            {"project": project, "status": status, "name": name, "limit": limit, "cursor": cursor}), then=as_dict)

    def create_run(self, name: Optional[str] = None, *, project: Optional[str] = None,
                   config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        for k, v in (("name", name), ("project", project), ("config", dict(config) if config is not None else None)):
            if v is not None:
                body[k] = v
        return self._call("POST", "/v1/rl/runs", body, then=as_dict)

    def run(self, run_id: str) -> Dict[str, Any]:
        """A run with its summary and recommended actions."""
        _need("run_id", run_id)
        return self._call("GET", f"/v1/rl/runs/{q(run_id)}", then=as_dict)

    def update_run(self, run_id: str, *, status: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
        _need("run_id", run_id)
        body = {k: v for k, v in (("status", status), ("name", name)) if v is not None}
        if not body:
            raise ValueError("give status or name")
        return self._call("PATCH", f"/v1/rl/runs/{q(run_id)}", body, then=as_dict)

    def rollouts(self, run_id: str, *, step: Optional[int] = None, failure_class: Optional[str] = None,
                 min_reward: Optional[float] = None, max_reward: Optional[float] = None,
                 sandbox_id: Optional[str] = None, limit: Optional[int] = None,
                 cursor: Optional[str] = None) -> Dict[str, Any]:
        """One page of a run's rollouts, newest first: ``{"rollouts": [...], "next_cursor"}``."""
        _need("run_id", run_id)
        return self._call("GET", f"/v1/rl/runs/{q(run_id)}/rollouts" + query({
            "step": step, "failure_class": failure_class, "min_reward": min_reward, "max_reward": max_reward,
            "sandbox_id": sandbox_id, "limit": limit, "cursor": cursor,
        }), then=as_dict)

    def export(self, run_id: str) -> bytes:
        """Every rollout of a run as JSON lines, oldest first."""
        _need("run_id", run_id)
        return self._call("GET", f"/v1/rl/runs/{q(run_id)}/export?format=jsonl", raw=True)

    def compare(self, run_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Two to four runs side by side: each ``{"run", "summary"}``."""
        ids = [run_ids] if isinstance(run_ids, str) else list(dict.fromkeys(run_ids))
        if not 2 <= len(ids) <= 4:
            raise ValueError("compare takes 2 to 4 different run ids")
        return self._call("GET", "/v1/rl/compare" + query({"ids": ids}), then=key("runs", list))

    def alerts(self, *, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Alert rules (``alerts``), the ``kinds`` a rule can watch and the webhook ``event`` it sends."""
        return self._call("GET", "/v1/rl/alerts" + query({"run_id": run_id}), then=as_dict)

    def create_alert(self, kind: str, threshold: float, *, run_id: Optional[str] = None,
                     window: Optional[int] = None) -> Dict[str, Any]:
        """A rule on one run (``run_id``) or every run; see :meth:`alerts` for the kinds."""
        _need("kind", kind)
        body: Dict[str, Any] = {"kind": kind, "threshold": threshold}
        if run_id is not None:
            body["run_id"] = run_id
        if window is not None:
            body["window"] = window
        return self._call("POST", "/v1/rl/alerts", body, then=key("alert", dict))

    def update_alert(self, alert_id: str, *, threshold: Optional[float] = None, window: Optional[int] = None,
                     enabled: Optional[bool] = None) -> Dict[str, Any]:
        """Change, pause (``enabled=False``) or resume a rule."""
        _need("alert_id", alert_id)
        body = {k: v for k, v in (("threshold", threshold), ("window", window), ("enabled", enabled)) if v is not None}
        if not body:
            raise ValueError("give threshold, window or enabled")
        return self._call("PATCH", f"/v1/rl/alerts/{q(alert_id)}", body, then=key("alert", dict))

    def delete_alert(self, alert_id: str) -> Dict[str, Any]:
        _need("alert_id", alert_id)
        return self._call("DELETE", f"/v1/rl/alerts/{q(alert_id)}", then=as_dict)


class AsyncCloudRL(AsyncCalls, CloudRL):
    """:class:`CloudRL` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# Functions


class CloudFunctions(Calls):
    """Functions on kosa8 Cloud (``cloud.functions``): deploy code, call it,
    map it over events, read its recent invocations."""

    @staticmethod
    def _path(name: str, suffix: str = "") -> str:
        _need("name", name)
        return f"/v1/functions/{q(name)}{suffix}"

    def deploy(self, name: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
        """Deploy (create or replace) a function; every deploy bumps its version."""
        return self._call("PUT", self._path(name), dict(spec))

    def list(self) -> List[Dict[str, Any]]:  # noqa: A003
        return self._call("GET", "/v1/functions", then=key("functions", list))

    def get(self, name: str) -> Dict[str, Any]:
        return self._call("GET", self._path(name))

    def delete(self, name: str) -> None:
        return self._call("DELETE", self._path(name), then=lambda _r: None)

    def invoke(self, name: str, event: Any = None) -> Dict[str, Any]:
        """Call it once. A handler that raises answers ``ok: False``, not an exception."""
        return self._call("POST", self._path(name, "/invoke"), {"event": event})

    def map(self, name: str, events: Sequence[Any], *, concurrency: Optional[int] = None) -> Dict[str, Any]:  # noqa: A003
        """One call per event (up to 100), results in input order."""
        body: Dict[str, Any] = {"events": list(events)}
        if concurrency is not None:
            body["concurrency"] = concurrency
        return self._call("POST", self._path(name, "/map"), body)

    def invocations(self, name: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Recent calls, newest first."""
        _need("name", name)
        if limit is not None:
            _whole("limit", limit, 1)
        return self._call("GET", f"/v1/functions/{q(name)}/invocations" + query({"limit": limit}),
                          then=key("invocations", list))


class AsyncCloudFunctions(AsyncCalls, CloudFunctions):
    """:class:`CloudFunctions` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# Serverless runs: POST /v1/run, GET /v1/runs, GET /v1/runs/{id}


#: The longest ``timeout_s`` a run takes (serverless.js RUN_MAX_TIMEOUT_S).
RUN_MAX_TIMEOUT_S = 3600


class CloudRuns(Calls):
    """One-call serverless runs (``cloud.runs``): code or a command in a fresh
    sandbox, output, exit code and files back, the sandbox deleted. Billed per
    second like any sandbox."""

    def create(
        self,
        code: Optional[str] = None,
        *,
        command: Union[str, Sequence[str], None] = None,
        language: Optional[str] = None,
        filename: Optional[str] = None,
        template: Optional[str] = None,
        files: Union[Mapping[str, str], Sequence[Mapping[str, Any]], None] = None,
        env: Optional[Mapping[str, str]] = None,
        size: Optional[str] = None,
        timeout_s: Optional[int] = None,
        outputs: Optional[Sequence[str]] = None,
        share: Union[bool, Mapping[str, Any], None] = None,
        repo: Union[str, Mapping[str, Any], None] = None,
    ) -> Dict[str, Any]:
        """Run ``code`` (written to ``filename``, default ``main.<ext>``) or
        ``command`` and wait for it. Returns the run: ``id``, ``status``
        (``ok``, ``failed``, ``timeout`` or ``error``), ``exit``, ``stdout``,
        ``stderr``, ``files`` (the ``outputs`` asked for, base64),
        ``duration_ms``, ``cost_usd``, ``view_url``. A command that exits
        non-zero is ``status: failed``, not an exception.

        ``repo`` runs it in a checkout of a repo of the org's connected
        GitHub (``"owner/name"``, ``"owner/name@ref"`` or ``{"name", "ref",
        "path", "depth"}``); the command runs in /workspace/<name>."""
        if code is None and command is None:
            raise ValueError("give code or command")
        body: Dict[str, Any] = {}
        if code is not None:
            body["code"] = code
        if command is not None:
            body["command"] = command if isinstance(command, str) else list(command)
        for k, v in (("language", language), ("filename", filename), ("template", template), ("size", size)):
            if v is not None:
                body[k] = v
        if files is not None:
            body["files"] = dict(files) if isinstance(files, Mapping) else [dict(f) for f in files]
        if env:
            body["env"] = dict(env)
        if timeout_s is not None:
            _whole("timeout_s", timeout_s, 1, RUN_MAX_TIMEOUT_S)
            body["timeout_s"] = timeout_s
        if outputs:
            body["outputs"] = list(outputs)
        if share is not None:
            body["share"] = share if isinstance(share, bool) else dict(share)
        if repo is not None:
            body["repo"] = repo_body(repo)
        # The answer comes when the command ends: wait past its timeout_s.
        wait = max(float(self._cloud.timeout), float(timeout_s if timeout_s is not None else 60) + 60.0)
        return self._call("POST", "/v1/run", body, then=key("run", dict), timeout=wait)

    def list(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:  # noqa: A003
        """The org's recent runs, newest first (kept 30 days)."""
        if limit is not None:
            _whole("limit", limit, 1, 100)
        return self._call("GET", "/v1/runs" + query({"limit": limit}), then=key("runs", list))

    def get(self, run_id: str) -> Dict[str, Any]:
        """One run, with the saved tail of its output."""
        _need("run_id", run_id)
        return self._call("GET", f"/v1/runs/{q(run_id)}", then=key("run", dict))

    def share(self, run_id: str, public: bool, *, code: Optional[bool] = None) -> Dict[str, Any]:
        """Publish (or unpublish) the run's read-only card; ``code`` shows its code too."""
        _need("run_id", run_id)
        body: Dict[str, Any] = {"public": bool(public)}
        if code is not None:
            body["code"] = bool(code)
        return self._call("POST", f"/v1/runs/{q(run_id)}/share", body, then=key("run", dict))


class AsyncCloudRuns(AsyncCalls, CloudRuns):
    """:class:`CloudRuns` for :class:`~kosa8.AsyncCloud`."""


# ---------------------------------------------------------------------------
# GitHub repos: connect once, start sandboxes and runs from a repo, push and
# open pull requests. kosa8 mints a token for one repo and one operation
# (at most an hour); it never enters the sandbox and every use is audited.

#: owner/name, as GitHub allows them.
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def _repo_name(name: Any) -> str:
    if not isinstance(name, str) or not REPO_NAME_RE.match(name):
        raise ValueError(f"repo must be owner/name (optionally @ref), got {name!r}")
    return name


def repo_body(spec: Union[str, Mapping[str, Any]]) -> Union[str, Dict[str, Any]]:
    """The ``repo`` field of a create or run, checked: ``"owner/name"`` or
    ``"owner/name@ref"`` stays a string; a mapping is sent as ``{name, ref?,
    path?, depth?}``."""
    if isinstance(spec, str):
        name, at, ref = spec.partition("@")
        _repo_name(name)
        if at and not ref:
            raise ValueError(f"repo {spec} has an empty ref after @")
        return spec
    if not isinstance(spec, Mapping):
        raise ValueError('repo must be "owner/name[@ref]" or {"name", "ref", "path", "depth"}')
    out: Dict[str, Any] = {"name": _repo_name(spec.get("name"))}
    for k in ("ref", "path"):
        if spec.get(k) is not None:
            out[k] = spec[k]
    if spec.get("depth") is not None:
        _whole("depth", spec["depth"], 1)
        out["depth"] = spec["depth"]
    return out


def push_body(branch: Optional[str] = None, message: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
    """The body of ``POST /v1/sandboxes/{id}/github/push``."""
    return {k: v for k, v in (("branch", branch), ("message", message), ("path", path)) if v is not None}


def pull_request_body(title: Optional[str] = None, body: Optional[str] = None, base: Optional[str] = None,
                      branch: Optional[str] = None, draft: Optional[bool] = None) -> Dict[str, Any]:
    """The body of ``POST /v1/sandboxes/{id}/github/pull-request``."""
    return {k: v for k, v in (("title", title), ("body", body), ("base", base), ("branch", branch), ("draft", draft))
            if v is not None}


def _repos_page(answer: Any) -> Dict[str, Any]:
    a = answer if isinstance(answer, dict) else {}
    return {"repos": a.get("repos") or [], "next_page": a.get("next_page")}


class CloudGitHub(Calls):
    """The org's connected GitHub (``cloud.github``). An admin connects it
    once (app.kosa8.com Settings, or ``kosa8 github connect``); listing repos
    needs only the normal sandbox scope."""

    def status(self) -> Dict[str, Any]:
        """``configured``, ``connected``, ``can_manage``, ``install_url``
        (admins only) and ``installations`` (each with ``manage_url`` and
        ``missing_permissions``)."""
        return self._call("GET", "/v1/github", then=as_dict)

    def repos_page(self, query: Optional[str] = None, *, page: Optional[int] = None) -> Dict[str, Any]:
        """One page of :meth:`repos`: ``{"repos": [...], "next_page": n or None}``."""
        return self._call("GET", "/v1/github/repos" + _query({"q": query or None, "page": page}), then=_repos_page)

    def repos(self, query: Optional[str] = None, *, page: Optional[int] = None) -> Any:
        """Repos the org's installations can see, most recently pushed first
        (``full_name``, ``private``, ``default_branch``, ``html_url``, ...);
        ``query`` filters by substring."""
        return self._call("GET", "/v1/github/repos" + _query({"q": query or None, "page": page}),
                          then=lambda a: _repos_page(a)["repos"])

    def branches(self, repo: str) -> Dict[str, Any]:
        """A repo's (``owner/name``) ``default_branch`` and ``branches`` (the first 100)."""
        owner, name = _repo_name(repo).split("/")
        return self._call("GET", f"/v1/github/repos/{q(owner)}/{q(name)}/branches", then=as_dict)


class AsyncCloudGitHub(AsyncCalls, CloudGitHub):
    """:class:`CloudGitHub` for :class:`~kosa8.AsyncCloud`."""


__all__ = [
    "CloudGitHub", "AsyncCloudGitHub", "repo_body", "REPO_NAME_RE",
    "CloudAccount", "AsyncCloudAccount",
    "CloudApiKeys", "AsyncCloudApiKeys",
    "CloudWebhooks", "AsyncCloudWebhooks",
    "CloudAudit", "AsyncCloudAudit",
    "CloudBenchmarks", "AsyncCloudBenchmarks",
    "CloudRL", "AsyncCloudRL",
    "CloudFunctions", "AsyncCloudFunctions",
    "CloudRuns", "AsyncCloudRuns",
]
