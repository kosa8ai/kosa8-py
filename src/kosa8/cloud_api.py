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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from ._calls import AsyncCalls, Calls, as_dict, key, q, query


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


__all__ = [
    "CloudAccount", "AsyncCloudAccount",
    "CloudApiKeys", "AsyncCloudApiKeys",
    "CloudWebhooks", "AsyncCloudWebhooks",
    "CloudAudit", "AsyncCloudAudit",
    "CloudBenchmarks", "AsyncCloudBenchmarks",
    "CloudRL", "AsyncCloudRL",
    "CloudFunctions", "AsyncCloudFunctions",
]
