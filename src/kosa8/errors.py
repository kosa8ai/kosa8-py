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


_BY_STATUS = {
    400: Kosa8BadRequestError,
    401: Kosa8AuthError,
    403: Kosa8PolicyError,
    404: Kosa8NotFoundError,
    409: Kosa8ConflictError,
    501: Kosa8UnsupportedError,
}


def error_from_response(
    status: int, body: Any, *, method: Optional[str] = None, path: Optional[str] = None
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
    )
    if status == 403 and _is_refusal(approval):
        return Kosa8ApprovalRequiredError(message, approval, **kw)
    return _BY_STATUS.get(status, Kosa8ApiError)(message, **kw)
