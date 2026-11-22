"""Typed errors.

Every error the daemon answers with carries its own words: ``daemon_message``
is the daemon's ``error`` field verbatim and ``remediation`` its
``remediation`` field (what to do about it). A refusal because the org policy
wants signed approvals also carries the refusal itself, under ``approval``,
exactly as the daemon sent it. Mirrors ``@kosa8/sdk``'s errors.ts.
"""

from __future__ import annotations

from typing import Any, Optional


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
    """The daemon answered with an HTTP error."""


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


class Kosa8NotFoundError(Kosa8ApiError):
    """404: no such sandbox, snapshot, container, ..."""


class Kosa8ConflictError(Kosa8ApiError):
    """409: a conflict, e.g. the sandbox exists but is not running."""


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
    """

    @property
    def upgrade_url(self) -> Optional[str]:
        return _body_str(self.body, "upgrade_url")

    @property
    def agent_message(self) -> Optional[str]:
        return _body_str(self.body, "agent_message")


class Kosa8OrgFrozenError(Kosa8ApiError):
    """423 ``org_frozen``: the organization is frozen in the Security Center.

    New sandboxes, commands, wakes and function calls are refused until an
    owner or admin unfreezes it. Retrying does not help.
    """


class Kosa8RateLimitError(Kosa8ApiError):
    """429: too many requests, or a quota reached (``quota_exceeded``).

    ``retry_after`` is the server's Retry-After in seconds, when it sent one.
    """

    retry_after: Optional[float] = None


class Kosa8ExecTimeoutError(Kosa8ApiError, Kosa8TimeoutError):
    """A kosa8 Cloud exec ran past the API's limit for a buffered command.

    A buffered exec runs at most 90 seconds; past that the API stops it and
    answers 504 ``exec_timeout`` (older servers: HTTP 502 ``host_failed`` "timed out").
    Run long work in the background, writing to /workspace, and poll it::

        box.exec("nohup sh -c 'make test > /workspace/test.log 2>&1' &")
    """


def _body_str(body: Any, key: str) -> Optional[str]:
    if isinstance(body, dict) and isinstance(body.get(key), str) and body[key]:
        return body[key]
    return None


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
    return _BY_STATUS.get(status, Kosa8ApiError)(message, **kw)


#: Failure classes, as kosa8 Cloud sends them in ``failure_class``.
#: ``error`` is the SDK's own: your code raised something that is not a kosa8 error.
FAILURE_CLASSES = ("ok", "exit_nonzero", "timeout", "infra", "quota", "client", "error")

_INFRA_CODES = frozenset({
    "host_failed", "not_ready", "host_gone", "host_lost", "host_removed", "capacity", "no_capacity",
    "placement", "placement_failed", "unavailable", "internal", "upstream",
})
_QUOTA_CODES = frozenset({
    "running_limit", "quota_exceeded", "plan_limit", "payment_required", "spending_cap_reached", "org_frozen",
})


def classify_failure(exc: BaseException) -> "tuple[str, bool]":
    """``(failure_class, retryable)`` for any exception.

    A kosa8 Cloud error body's ``failure_class`` and ``retryable`` are used as
    sent. Otherwise: no answer, a 5xx (not 501) or a host/capacity code is
    ``infra`` (retryable); an exec past its limit is ``timeout``; 402, 423 and
    plan/running limits are ``quota``; 429 is ``quota`` and retryable unless it
    is a used-up quota; any other 4xx and 501 are ``client``. An exception that
    is not a :class:`Kosa8Error` is ``error`` (not retryable): a bug in your code.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("failure_class"), str):
        fc = body["failure_class"]
        rt = body.get("retryable")
        return fc, bool(rt) if isinstance(rt, bool) else fc == "infra"
    if not isinstance(exc, Kosa8Error):
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return "infra", True
        return "error", False
    if isinstance(exc, Kosa8ExecTimeoutError):
        return "timeout", False
    if isinstance(exc, (Kosa8ConnectionError, Kosa8TimeoutError)):
        return "infra", True
    status, code = exc.status, exc.code
    if code in _INFRA_CODES:
        return "infra", True
    if status == 429:
        return "quota", code != "quota_exceeded"
    if code in _QUOTA_CODES or status in (402, 423):
        return "quota", False
    if status is not None and status >= 500 and status != 501:
        return "infra", True
    if status is not None:
        return "client", False
    return "error", False
