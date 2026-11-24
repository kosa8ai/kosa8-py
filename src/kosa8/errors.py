"""Typed errors.

Every error the daemon answers with carries its own words: ``daemon_message``
is the daemon's ``error`` field verbatim and ``remediation`` its
``remediation`` field (what to do about it). A refusal because the org policy
wants signed approvals also carries the refusal itself, under ``approval``,
exactly as the daemon sent it. Mirrors ``@kosa8/sdk``'s errors.ts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class Kosa8Error(Exception):
    """Base class of every error this SDK raises."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        daemon_message: Optional[str] = None,
        remediation: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        body: Any = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: HTTP status the daemon answered with, when there was an answer.
        self.status = status
        #: The daemon's ``error`` text, verbatim.
        self.daemon_message = daemon_message
        #: The daemon's ``remediation`` text: what to do about it.
        self.remediation = remediation
        self.method = method
        self.path = path
        #: The decoded response body, when there was one.
        self.body = body
        #: kosa8 Cloud's stable error code (payment_required, quota_exceeded, ...).
        self.code = code
        #: The request's id (kosa8 Cloud's ``X-Request-Id``, else Cloudflare's
        #: ``cf-ray``): quote it to support. ``None`` when there was no answer.
        self.request_id = request_id
        #: Seconds the server asked to wait before trying again (its
        #: ``Retry-After`` header, else the body's ``retry_after_s``); ``None``
        #: when it said nothing.
        self.retry_after = retry_after
        # Set on kosa8 Cloud API answers: str() then adds the status, code,
        # retry hint and request id after the server's sentence.
        self._api_answer = False

    def __str__(self) -> str:
        if not getattr(self, "_api_answer", False):
            return super().__str__()
        return f"{self.message} [{'; '.join(_answer_facts(self))}]"

    @property
    def retry_after_s(self) -> Optional[float]:
        """The same as :attr:`retry_after`: seconds to wait before trying again."""
        return self.retry_after

    @property
    def failure_class(self) -> str:
        """What kind of failure this is, for an RL trainer deciding whether to
        mask a rollout: ``infra``, ``timeout``, ``quota``, ``client`` (or
        ``exit_nonzero``). The server's own ``failure_class`` wins when it sent one;
        otherwise it is worked out from the status and code the same way."""
        return classify_failure(self)[0]

    @property
    def retryable(self) -> bool:
        """Whether trying again (after a pause) can help: infra failures and
        rate limits, not quota, payment or a bad request."""
        return classify_failure(self)[1]


class Kosa8ConnectionError(Kosa8Error):
    """The daemon could not be reached (no socket, connection refused, ...)."""


class Kosa8ApiError(Kosa8Error):
    """The daemon (or kosa8 Cloud) answered with an HTTP error.

    kosa8 Cloud's refusal fields are read from the answer's body, ``None``
    when it did not send them: ``upgrade_url`` and ``agent_message`` (402,
    and 429 ``quota_exceeded``), ``request_url`` (429 ``quota_exceeded``),
    and for a 402 ``plan_limit`` / ``plan_feature`` ``limit_name``,
    ``limit``, ``used``, ``plan``, ``next_plan``, ``feature``, ``options``
    and ``pack_url``.
    """

    @property
    def upgrade_url(self) -> Optional[str]:
        """Where the person adds a payment method or credit, or raises the cap."""
        return _body_str(self.body, "upgrade_url")

    @property
    def agent_message(self) -> Optional[str]:
        """A sentence an AI agent should relay to its person word for word."""
        return _body_str(self.body, "agent_message")

    @property
    def request_url(self) -> Optional[str]:
        """429 ``quota_exceeded``: where to ask for a higher limit."""
        return _body_str(self.body, "request_url")

    @property
    def limit_name(self) -> Optional[str]:
        """402 ``plan_limit``: ``sandbox_hours``, ``running``, ``size``, ``persistent`` or ``snapshots``."""
        return _body_str(self.body, "limit_name")

    @property
    def limit(self) -> Optional[float]:
        """402 ``plan_limit``: the plan's limit."""
        return _body_num(self.body, "limit")

    @property
    def used(self) -> Optional[float]:
        """402 ``plan_limit``: what the organization has used."""
        return _body_num(self.body, "used")

    @property
    def plan(self) -> Optional[str]:
        """402 ``plan_limit`` / ``plan_feature``: the organization's plan."""
        return _body_str(self.body, "plan")

    @property
    def next_plan(self) -> Optional[str]:
        """The first plan that includes more (``None``: contact sales, or not sent)."""
        return _body_str(self.body, "next_plan")

    @property
    def feature(self) -> Optional[str]:
        """402 ``plan_feature``: the feature the plan lacks."""
        return _body_str(self.body, "feature")

    @property
    def pack_url(self) -> Optional[str]:
        """402 ``plan_limit``: where to add the add-on pack for this limit."""
        return _body_str(self.body, "pack_url")

    @property
    def options(self) -> List[Dict[str, Any]]:
        """402 ``plan_limit``: the ways past the limit, cheaper first (``kind``
        ``pack`` or ``upgrade``, ``label``, ``monthly_usd``, ``url``); ``[]``
        when none were sent."""
        o = self.body.get("options") if isinstance(self.body, dict) else None
        return [x for x in o if isinstance(x, dict)] if isinstance(o, list) else []

    @property
    def connect_url(self) -> Optional[str]:
        """409 ``github_not_connected``: where an admin connects GitHub."""
        return _body_str(self.body, "connect_url")

    @property
    def settings_url(self) -> Optional[str]:
        """409 ``github_permissions``: where on GitHub to approve the permissions kosa8 lacks."""
        return _body_str(self.body, "settings_url")

    @property
    def missing_permissions(self) -> List[str]:
        """409 ``github_permissions``: the GitHub App permissions missing
        (``contents:write``, ``pull_requests:write``, ...); ``[]`` otherwise."""
        m = self.body.get("missing") if isinstance(self.body, dict) else None
        return [x for x in m if isinstance(x, str)] if isinstance(m, list) else []


class Kosa8BadRequestError(Kosa8ApiError):
    """400: the request was malformed or a value was refused."""


class Kosa8AuthError(Kosa8ApiError):
    """401: a bearer token (remote worker) or agent token was missing or refused."""


class Kosa8PolicyError(Kosa8ApiError):
    """403: refused by policy (org policy, registry allow-list, labels, ...)."""


class Kosa8ApprovalRequiredError(Kosa8PolicyError):
    """403 with a signed-approval refusal.

    The org policy wants people other than the caller to approve this exact
    action. Get the approval id from a human and repeat the call with
    ``approval=id``. ``approval`` holds the daemon's refusal (``why``,
    ``request_command``, ``signers``, ...).
    """

    def __init__(self, message: str, approval: dict, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.approval = approval


class Kosa8HumanApprovalRequiredError(Kosa8PolicyError):
    """403 ``human_required``: only a person can do this (turn a bot on, resume
    every bot, decide an approval). Capability tokens, MCP and org keys bound to
    no member are never a person. An agent never approves its own action; a
    person approves in the web app (app.kosa8.com/app#autopilot), desktop, CLI
    or Slack. There is no way around it from code."""


#: Short name for :class:`Kosa8HumanApprovalRequiredError`.
HumanApprovalRequired = Kosa8HumanApprovalRequiredError


class Kosa8NotFoundError(Kosa8ApiError):
    """404: no such sandbox, snapshot, container, ..."""


class Kosa8ConflictError(Kosa8ApiError):
    """409: a conflict, e.g. the sandbox exists but is not running."""


class Kosa8GitHubNotConnectedError(Kosa8ConflictError):
    """409 ``github_not_connected``: the org has not connected GitHub; an admin connects it at ``connect_url``."""


class Kosa8GitHubPermissionsError(Kosa8ConflictError):
    """409 ``github_permissions``: the kosa8 GitHub App lacks ``missing_permissions``; approve them at ``settings_url``."""


class Kosa8NoChangesError(Kosa8ConflictError):
    """409 ``no_changes``: nothing changed in the sandbox's repo, so there is nothing to push."""


class Kosa8RepoNotFoundError(Kosa8NotFoundError):
    """404 ``repo_not_found``: the repo is not visible to the org's GitHub installations (or does not exist)."""


class Kosa8GitHubUnavailableError(Kosa8ApiError):
    """502 ``github_unavailable``: GitHub did not answer or rate-limited kosa8; try again after ``retry_after`` seconds."""


class Kosa8UnsupportedError(Kosa8ApiError):
    """501: the platform cannot do this (sandboxes need macOS on Apple silicon or Linux with KVM)."""


class Kosa8StreamError(Kosa8Error):
    """A streamed operation ended with an error frame rather than an exit code.

    Output produced before the error is kept in ``stdout`` and ``stderr``.
    """

    def __init__(self, message: str, stdout: str = "", stderr: str = "", **kw: Any) -> None:
        super().__init__(message, **kw)
        self.stdout = stdout
        self.stderr = stderr


class Kosa8CheckpointError(Kosa8Error):
    """A client-side checkpoint operation failed (no checkpoint, bad name, ...)."""


class Kosa8TimeoutError(Kosa8Error):
    """A call took longer than the timeout it was given."""


class Kosa8ProtocolError(Kosa8Error):
    """The daemon sent a frame stream line that is not valid NDJSON."""


def _is_refusal(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("why"), str)


class Kosa8PaymentRequiredError(Kosa8ApiError):
    """402: kosa8 Cloud refused new work: no payment method on file
    (``payment_required``), the spending cap is reached
    (``spending_cap_reached``), or a paid Store item's price was not accepted.

    ``upgrade_url`` is where the person adds a payment method or credit, and
    ``agent_message`` a sentence an AI agent should relay to its person word
    for word (stop, don't retry). Both are ``None`` when the cloud sent none.
    A ``plan_limit`` also carries ``limit_name``, ``limit``, ``used``,
    ``plan``, ``next_plan``, ``options`` and ``pack_url`` (see
    :class:`Kosa8ApiError`).
    """


class Kosa8OrgFrozenError(Kosa8ApiError):
    """423 ``org_frozen``: the organization is frozen in the Security Center.

    New sandboxes, commands, wakes and function calls are refused until an
    owner or admin unfreezes it. Retrying does not help.
    """


class Kosa8RateLimitError(Kosa8ApiError):
    """429: too many requests, or a quota reached (``quota_exceeded``).

    ``retry_after`` is the server's Retry-After in seconds, when it sent one.
    A ``quota_exceeded`` also carries ``agent_message`` (relay it to the
    person) and ``request_url`` (where to ask for a higher limit).
    """


class Kosa8ExecTimeoutError(Kosa8ApiError, Kosa8TimeoutError):
    """A kosa8 Cloud exec ran past the API's limit for a buffered command.

    A buffered exec runs at most 90 seconds; past that the API stops it and
    answers 504 ``exec_timeout`` (older servers: HTTP 502 ``host_failed`` "timed out").
    Run long work in the background, writing to /workspace, and poll it::

        box.exec("nohup sh -c 'make test > /workspace/test.log 2>&1' &")
    """


def _seconds(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _answer_facts(e: "Kosa8Error") -> "list[str]":
    """What str() adds to a kosa8 Cloud error after the server's sentence:
    ``HTTP 503 no_capacity; retry in 10 s; request a5e7...-BOS``."""
    facts = [" ".join(x for x in (f"HTTP {e.status}" if e.status else "", e.code or "") if x) or "no answer"]
    if e.retry_after is not None and e.retry_after > 0:
        facts.append(f"retry in {_seconds(e.retry_after)} s")
    elif e.status is not None and e.retryable:
        facts.append("retryable")
    upgrade = _body_str(e.body, "upgrade_url")
    if e.status == 402 and upgrade and upgrade not in e.message:
        facts.append(f"upgrade: {upgrade}")
    if e.request_id:
        facts.append(f"request {e.request_id}")
    return facts


def _body_str(body: Any, key: str) -> Optional[str]:
    if isinstance(body, dict) and isinstance(body.get(key), str) and body[key]:
        return body[key]
    return None


def _body_num(body: Any, key: str) -> Optional[float]:
    v = body.get(key) if isinstance(body, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_BY_STATUS = {
    400: Kosa8BadRequestError,
    401: Kosa8AuthError,
    403: Kosa8PolicyError,
    404: Kosa8NotFoundError,
    402: Kosa8PaymentRequiredError,
    409: Kosa8ConflictError,
    423: Kosa8OrgFrozenError,
    429: Kosa8RateLimitError,
    501: Kosa8UnsupportedError,
}

_BY_CODE = {
    (409, "github_not_connected"): Kosa8GitHubNotConnectedError,
    (409, "github_permissions"): Kosa8GitHubPermissionsError,
    (409, "no_changes"): Kosa8NoChangesError,
    (404, "repo_not_found"): Kosa8RepoNotFoundError,
    (403, "human_required"): Kosa8HumanApprovalRequiredError,
}


def error_from_response(
    status: int,
    body: Any,
    *,
    method: Optional[str] = None,
    path: Optional[str] = None,
    code: Optional[str] = None,
) -> Kosa8ApiError:
    """Build the typed error for an HTTP error answer.

    The message is the daemon's own, with its remediation in parentheses, the
    way the kosa8 CLI and MCP server print it.
    """
    daemon_message: Optional[str] = None
    remediation: Optional[str] = None
    approval: Any = None
    if isinstance(body, dict):
        if isinstance(body.get("error"), str) and body["error"]:
            daemon_message = body["error"]
        elif isinstance(body.get("message"), str) and body["message"]:
            daemon_message = body["message"]
        if isinstance(body.get("remediation"), str) and body["remediation"]:
            remediation = body["remediation"]
        approval = body.get("approval")
    elif isinstance(body, str) and body.strip():
        daemon_message = body.strip()
    base = daemon_message or f"request failed with HTTP {status}"
    message = f"{base} ({remediation})" if remediation else base
    kw = dict(
        status=status,
        daemon_message=daemon_message,
        remediation=remediation,
        body=body,
        method=method,
        path=path,
        code=code,
    )
    if status == 403 and _is_refusal(approval):
        return Kosa8ApprovalRequiredError(message, approval, **kw)
    by_code = _BY_CODE.get((status, code)) or (Kosa8GitHubUnavailableError if code == "github_unavailable" else None)
    if by_code is not None:
        return by_code(message, **kw)
    return _BY_STATUS.get(status, Kosa8ApiError)(message, **kw)


#: Failure classes, as kosa8 Cloud sends them in ``failure_class``.
#: ``error`` is the SDK's own: your code raised something that is not a kosa8 error.
FAILURE_CLASSES = ("ok", "exit_nonzero", "timeout", "oom", "infra", "quota", "client", "error")

# The server's lists (workers/cloud-api/src/failures.js), for an error body
# that came without failure_class (an older Worker, a proxy's answer).
_INFRA_CODES = frozenset({
    "host_failed", "not_ready", "no_capacity", "host_unreachable", "host_unavailable", "host_busy", "host_restarting",
    "wake_failed", "try_again", "internal", "kernel_failed", "egress_failed", "dns_unreachable", "host_lost",
    "host_removed", "insufficient_storage", "starting", "database_busy",
})
#: Refusals that pass with time whatever the body says: 503 ``database_busy``
#: (the API's database is overloaded) and 409 ``template_building`` (the
#: template is being built; ``retry_after_s`` is its ETA).
_ALWAYS_RETRYABLE = frozenset({"database_busy", "template_building"})
_RATE_CODES = frozenset({"rate_limited", "slow_down", "too_soon"})
_QUOTA_CODES = frozenset({
    "quota_exceeded", "running_limit", "plan_required", "plan_limit", "plan_feature", "payment_required",
    "payment_failed", "payment_declined", "spending_cap_reached", "spend_cap", "starter_limit", "snapshot_limit",
    "org_frozen", "org_suspended", "too_many_runs",
})
#: A bad parameter is the caller's to fix, whatever it names (invalid_timeout
#: is not a timeout): failures.js checks this before the timeout rule.
_CLIENT_CODE_RE = re.compile(r"^(invalid|bad|missing|unknown)_")
#: A duplicate of an Idempotency-Key request that is still running: asking
#: again (after its ``retry_after_s``) gets the first one's answer.
IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"


def failure_of(status: Optional[int], code: Optional[str] = None) -> "tuple[str, bool]":
    """``(failure_class, retryable)`` for an HTTP status and error code, as
    kosa8 Cloud works it out (``failureOf`` in its failures.js)."""
    c = code if isinstance(code, str) else ""
    if status is None or status < 400:
        return "ok", False
    if c == "exec_timeout":
        return "timeout", False
    if c == IDEMPOTENCY_IN_PROGRESS:
        return "infra", True
    if 400 <= status < 500 and _CLIENT_CODE_RE.match(c):
        return "client", False
    if "timeout" in c:
        return "timeout", True
    if c in _RATE_CODES:
        return "quota", True
    if c in _QUOTA_CODES or status in (402, 423):
        return "quota", False
    if status == 429:
        return "quota", c != "quota_exceeded"
    if c in ("not_available", "not_supported") or status == 501:
        return "infra", False
    if c in _INFRA_CODES or status >= 500:
        return "infra", True
    return "client", False


def classify_failure(exc: BaseException) -> "tuple[str, bool]":
    """``(failure_class, retryable)`` for any exception.

    A kosa8 Cloud error body's ``failure_class`` and ``retryable`` are used as
    sent. Otherwise the server's rules (:func:`failure_of`): no answer, a 5xx
    (not 501) or a host/capacity code is ``infra`` (retryable); an exec past
    its limit is ``timeout``; 402, 423 and plan/running limits are ``quota``;
    429 is ``quota`` and retryable unless it is a used-up quota; 501 is
    ``infra`` but not retryable (the host lacks the feature); any other 4xx is
    ``client``. ``409 idempotency_in_progress`` is retryable: the first
    request with that key is still running; so are ``503 database_busy`` and
    ``409 template_building``, and any answer whose body says
    ``retryable: true``. An exception that is not a
    :class:`Kosa8Error` is ``error`` (not retryable): a bug in your code.
    """
    if isinstance(exc, Kosa8Error) and exc.code == IDEMPOTENCY_IN_PROGRESS:
        return "infra", True
    if isinstance(exc, Kosa8Error) and exc.code in _ALWAYS_RETRYABLE:
        return "infra", True
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("failure_class"), str):
        fc = body["failure_class"]
        rt = body.get("retryable")
        return fc, bool(rt) if isinstance(rt, bool) else fc == "infra"
    if isinstance(body, dict) and isinstance(body.get("retryable"), bool) and isinstance(exc, Kosa8Error) \
            and exc.status is not None:
        return failure_of(exc.status, exc.code)[0], body["retryable"]
    if not isinstance(exc, Kosa8Error):
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return "infra", True
        return "error", False
    if isinstance(exc, Kosa8ExecTimeoutError):
        return "timeout", False
    if isinstance(exc, (Kosa8ConnectionError, Kosa8TimeoutError)):
        return "infra", True
    if exc.status is None:
        return "error", False
    return failure_of(exc.status, exc.code)
