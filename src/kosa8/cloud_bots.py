# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Autopilot: bots that watch the org's sandboxes, spend and agents and act
(``cloud.bots``), and the approvals their T3 actions wait on
(``cloud.approvals``). :mod:`kosa8.cloud` wires these onto
:class:`~kosa8.Kosa8Cloud` and :mod:`kosa8.aio` onto :class:`~kosa8.AsyncCloud`,
where every call is a coroutine. Answers are the API's own dicts.

A bot starts as a ``draft``; turning it on (:meth:`CloudBots.enable`),
resuming every bot (:meth:`CloudBots.resume_all`) and deciding an approval
need a person's credential (a dashboard session or a member's API key, such as
the one ``kosa8 login`` makes). Anything else (a capability token, MCP, an org
key bound to no member) gets :class:`~kosa8.Kosa8HumanApprovalRequiredError`
(403 ``human_required``). An agent never approves its own action; a person
approves in the web app, desktop, CLI or Slack.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from ._calls import AsyncCalls, Calls, as_dict, key, q, query

#: The longest sentence :meth:`CloudBots.compile` takes.
COMPILE_MAX_CHARS = 500

#: Approval states :meth:`CloudApprovals.list` filters on.
APPROVAL_STATES = ("pending", "decided", "all")


def _need(name: str, v: Any) -> None:
    if not isinstance(v, str) or not v:
        raise ValueError(f"{name} is required")


def _limit(v: Optional[int], hi: int = 100) -> None:
    if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1 or v > hi):
        raise ValueError(f"limit must be a whole number from 1 to {hi}, got {v!r}")


def _one_of(template: Optional[str], spec: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if (template is None) == (spec is None):
        raise ValueError("give exactly one of template or spec")
    if template is not None:
        _need("template", template)
        return {"template": template}
    if not isinstance(spec, Mapping):
        raise ValueError("spec must be a mapping (triggers, actions, scope, ...)")
    return {"spec": dict(spec)}


def _put(body: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    for k, v in fields.items():
        if v is not None:
            body[k] = dict(v) if isinstance(v, Mapping) else v
    return body


class CloudBots(Calls):
    """The org's Autopilot bots (``cloud.bots``): start from a ranked template
    or a spec, see the plan and a dry run over the last 7 days of real events,
    then a person turns the bot on. Bots never touch sandboxes labelled
    ``protected`` or ``env=prod``.

    :meth:`enable` and :meth:`resume_all` need a person's credential and raise
    :class:`~kosa8.Kosa8HumanApprovalRequiredError` otherwise; an agent can
    create drafts, dry-run, disable and :meth:`pause_all`, never switch a bot on."""

    def __init__(self, cloud: Any, approvals: Optional["CloudApprovals"] = None) -> None:
        super().__init__(cloud)
        #: The same as ``cloud.approvals``.
        self.approvals = approvals if approvals is not None else CloudApprovals(cloud)

    # -- bots

    def list(self) -> Dict[str, Any]:  # noqa: A003
        """``{"bots": [...], "paused_all": bool, "paused_all_at", "pending_approvals": n}``."""
        return self._call("GET", "/v1/bots", then=as_dict)

    def templates(self) -> List[Dict[str, Any]]:
        """Ready-made bots ranked for this org (``rank``; ``why`` when an org signal moved one up)."""
        return self._call("GET", "/v1/bot-templates", then=key("templates", list))

    def template(self, template_id: str) -> Dict[str, Any]:
        """One template, with its ``spec`` and ``plan``."""
        _need("template_id", template_id)
        return self._call("GET", f"/v1/bot-templates/{q(template_id)}", then=key("template", dict))

    def plan(self, *, template: Optional[str] = None, spec: Optional[Mapping[str, Any]] = None,
             name: Optional[str] = None, replay: Optional[bool] = None) -> Dict[str, Any]:
        """What a bot from ``template`` or ``spec`` would do: ``{"plan", "spec", "replay"?}``.
        ``replay=True`` adds what it would have done over the last 7 days. Saves nothing."""
        body = _put(_one_of(template, spec), name=name, replay=replay)
        return self._call("POST", "/v1/bots/plan", body, then=as_dict)

    def compile(self, text: str) -> Dict[str, Any]:
        """A sentence (at most 500 characters) to ``{"spec", "plan", "source", "notes"}``.
        Never saves and never turns anything on."""
        _need("text", text)
        if len(text) > COMPILE_MAX_CHARS:
            raise ValueError(f"text must be at most {COMPILE_MAX_CHARS} characters, got {len(text)}")
        return self._call("POST", "/v1/bots/compile", {"text": text}, then=as_dict)

    def create(self, *, template: Optional[str] = None, spec: Optional[Mapping[str, Any]] = None,
               name: Optional[str] = None, sponsor: Optional[str] = None, budget_usd: Optional[float] = None,
               scope: Optional[Mapping[str, Any]] = None, notify: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """A new bot in state ``draft`` from exactly one of ``template`` (an id such as
        ``"spend-guard"``) or ``spec``. ``sponsor`` (a member's email) defaults to the
        person calling; a non-human caller must give one."""
        body = _put(_one_of(template, spec), name=name, sponsor=sponsor, budget_usd=budget_usd,
                    scope=scope, notify=notify)
        return self._call("POST", "/v1/bots", body, then=key("bot", dict))

    def get(self, bot_id: str) -> Dict[str, Any]:
        """One bot, with ``spec``, ``plan`` and ``versions``."""
        _need("bot_id", bot_id)
        return self._call("GET", f"/v1/bots/{q(bot_id)}", then=key("bot", dict))

    def update(self, bot_id: str, *, name: Optional[str] = None, spec: Optional[Mapping[str, Any]] = None,
               sponsor: Optional[str] = None, budget_usd: Optional[float] = None,
               scope: Optional[Mapping[str, Any]] = None, notify: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Change a bot. A new ``spec`` is a new version; a non-human changing an ``on``
        bot's spec sends it back to ``draft``."""
        _need("bot_id", bot_id)
        body = _put({}, name=name, spec=spec, sponsor=sponsor, budget_usd=budget_usd, scope=scope, notify=notify)
        if not body:
            raise ValueError("nothing to update: give name, spec, sponsor, budget_usd, scope or notify")
        return self._call("PATCH", f"/v1/bots/{q(bot_id)}", body, then=key("bot", dict))

    def delete(self, bot_id: str) -> Dict[str, Any]:
        """Delete a bot (its runs are kept; its pending approvals are rejected)."""
        _need("bot_id", bot_id)
        return self._call("DELETE", f"/v1/bots/{q(bot_id)}", then=as_dict)

    def enable(self, bot_id: str) -> Dict[str, Any]:
        """Turn a bot on (state ``on``). Needs a person's credential:
        :class:`~kosa8.Kosa8HumanApprovalRequiredError` otherwise."""
        _need("bot_id", bot_id)
        return self._call("POST", f"/v1/bots/{q(bot_id)}/enable", {}, then=key("bot", dict))

    def disable(self, bot_id: str) -> Dict[str, Any]:
        """Turn a bot off (state ``paused``). Any credential with ``sandboxes:write``."""
        _need("bot_id", bot_id)
        return self._call("POST", f"/v1/bots/{q(bot_id)}/disable", {}, then=key("bot", dict))

    # -- runs

    def run(self, bot_id: str, *, dry_run: bool = True) -> Dict[str, Any]:
        """Run a bot now. A dry run (the default) says what it would do now plus a
        ``replay`` over the last 7 days; it changes nothing and is never billed.
        ``dry_run=False`` evaluates the bot's triggers for real (the bot must be on)."""
        _need("bot_id", bot_id)
        return self._call("POST", f"/v1/bots/{q(bot_id)}/runs", {"dry_run": bool(dry_run)}, then=key("run", dict))

    def runs(self, bot_id: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """A bot's runs, newest first."""
        _need("bot_id", bot_id)
        _limit(limit)
        return self._call("GET", f"/v1/bots/{q(bot_id)}/runs" + query({"limit": limit}), then=key("runs", list))

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """One run (``brun_...``) with its steps."""
        _need("run_id", run_id)
        return self._call("GET", f"/v1/bot-runs/{q(run_id)}", then=key("run", dict))

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        """Cancel a run; its pending approvals are rejected."""
        _need("run_id", run_id)
        return self._call("POST", f"/v1/bot-runs/{q(run_id)}/cancel", {}, then=key("run", dict))

    def undo_step(self, run_id: str, step_key: str) -> Dict[str, Any]:
        """Undo one step that has an undo (wake the sandbox, remove the bot's own
        threat block, lift a label block)."""
        _need("run_id", run_id)
        _need("step_key", step_key)
        return self._call("POST", f"/v1/bot-runs/{q(run_id)}/steps/{q(step_key)}/undo", {}, then=key("run", dict))

    # -- the whole org

    def pause_all(self) -> Dict[str, Any]:
        """Stop every bot in the org (``admin``; any credential, so an agent can stop
        everything). ``{"paused_all", "at"}``."""
        return self._call("POST", "/v1/bots/pause-all", {}, then=as_dict)

    def resume_all(self) -> Dict[str, Any]:
        """Undo :meth:`pause_all` (``admin`` and a person's credential:
        :class:`~kosa8.Kosa8HumanApprovalRequiredError` otherwise)."""
        return self._call("POST", "/v1/bots/resume-all", {}, then=as_dict)


class CloudApprovals(Calls):
    """T3 bot actions waiting on a person (``cloud.approvals``, also
    ``cloud.bots.approvals``). Approvals expire after 24 hours (as rejected).

    Deciding needs a person's credential whose role covers the action; a
    capability token, MCP or an org key bound to no member gets
    :class:`~kosa8.Kosa8HumanApprovalRequiredError`. An agent never approves its
    own action; a person approves in the web app, desktop, CLI or Slack."""

    def list(self, state: str = "pending", *, limit: Optional[int] = None) -> List[Dict[str, Any]]:  # noqa: A003
        """Approvals in ``state`` (``pending``, ``decided`` or ``all``)."""
        if state not in APPROVAL_STATES:
            raise ValueError(f"state must be one of {', '.join(APPROVAL_STATES)}, got {state!r}")
        _limit(limit, 1000)
        return self._call("GET", "/v1/approvals" + query({"state": state, "limit": limit}),
                          then=key("approvals", list))

    def get(self, approval_id: str) -> Dict[str, Any]:
        """One approval (``appr_...``)."""
        _need("approval_id", approval_id)
        return self._call("GET", f"/v1/approvals/{q(approval_id)}", then=key("approval", dict))

    def _decide(self, approval_id: str, decision: str, reason: Optional[str],
                edited_args: Optional[Mapping[str, Any]]) -> Any:
        _need("approval_id", approval_id)
        body = _put({"decision": decision}, reason=reason, edited_args=edited_args)
        return self._call("POST", f"/v1/approvals/{q(approval_id)}/decide", body, then=as_dict)

    def approve(self, approval_id: str, *, reason: Optional[str] = None,
                edited_args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Approve (``{"approval", "run"}``), optionally with ``edited_args`` (checked
        against the action's schema). A person's credential only; 409 ``expired`` or
        ``decided`` when it is too late."""
        return self._decide(approval_id, "approve", reason, edited_args)

    def reject(self, approval_id: str, *, reason: Optional[str] = None) -> Dict[str, Any]:
        """Reject (``{"approval", "run"}``). A person's credential only."""
        return self._decide(approval_id, "reject", reason, None)


class AsyncCloudApprovals(AsyncCalls, CloudApprovals):
    """:class:`CloudApprovals` for :class:`~kosa8.AsyncCloud`."""


class AsyncCloudBots(AsyncCalls, CloudBots):
    """:class:`CloudBots` for :class:`~kosa8.AsyncCloud`."""

    def __init__(self, cloud: Any, approvals: Optional[AsyncCloudApprovals] = None) -> None:
        super().__init__(cloud, approvals if approvals is not None else AsyncCloudApprovals(cloud))


__all__ = [
    "CloudBots", "AsyncCloudBots", "CloudApprovals", "AsyncCloudApprovals",
    "COMPILE_MAX_CHARS", "APPROVAL_STATES",
]
