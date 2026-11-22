# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""More kosa8 Cloud org features: Burst Passes, capability tokens and desktop
recordings. :mod:`kosa8.cloud` wires these onto :class:`~kosa8.Kosa8Cloud` as
``cloud.burst``, ``cloud.tokens`` and ``cloud.recordings`` (and
:mod:`kosa8.aio` onto :class:`~kosa8.AsyncCloud`, where every call is a
coroutine). Answers are the API's own dicts.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: The scopes a capability token can carry.
TOKEN_SCOPES = frozenset({"sandboxes:read", "sandboxes:write", "functions:invoke"})


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


def _check_range(name: str, v: Any, lo: int, hi: int) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}, got {v!r}")


def _burst_body(quantity: int) -> Dict[str, Any]:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ValueError("quantity must be a whole number of at least 1")
    return {"quantity": quantity}


def _token_body(
    scopes: Sequence[str],
    sandbox_ids: Optional[Sequence[str]],
    labels: Optional[Mapping[str, str]],
    ttl_s: Optional[int],
    name: Optional[str],
) -> Dict[str, Any]:
    if isinstance(scopes, str):
        scopes = [scopes]
    if not scopes:
        raise ValueError("a token needs at least one scope")
    body: Dict[str, Any] = {"scopes": list(scopes)}
    if sandbox_ids is not None:
        ids = [sandbox_ids] if isinstance(sandbox_ids, str) else list(sandbox_ids)
        if not 1 <= len(ids) <= 100:
            raise ValueError("sandbox_ids takes 1 to 100 ids")
        body["sandbox_ids"] = ids
    if labels is not None:
        body["labels"] = dict(labels)
    if ttl_s is not None:
        _check_range("ttl_s", ttl_s, 60, 86_400)
        body["ttl_s"] = ttl_s
    if name is not None:
        body["name"] = name
    return body


def _recordings_path(sandbox_id: Optional[str], limit: Optional[int]) -> str:
    q: Dict[str, str] = {}
    if sandbox_id is not None:
        q["sandbox_id"] = sandbox_id
    if limit is not None:
        _check_range("limit", limit, 1, 200)
        q["limit"] = str(limit)
    return "/v1/recordings" + (f"?{urllib.parse.urlencode(q)}" if q else "")


def _recordings_of(answer: Any) -> Dict[str, Any]:
    r = answer if isinstance(answer, dict) else {}
    return {
        "recordings": list(r.get("recordings") or []),
        "configured": r.get("configured") is True,
        "retention_days": r.get("retention_days"),
    }


def _link_body(ttl_s: Optional[int]) -> Dict[str, Any]:
    if ttl_s is None:
        return {}
    _check_range("ttl_s", ttl_s, 60, 86_400)
    return {"ttl_s": ttl_s}


# ---------------------------------------------------------------------------
# Burst Passes


class CloudBurst:
    """Burst Passes (``cloud.burst``): each raises the running limit for its
    hours, stacking up to the pass's ``max_active``."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def get(self) -> Dict[str, Any]:
        """The pass, the passes held and those running (``GET /v1/billing/burst``):
        ``available``, ``pass`` (None when not sold here), ``held``, ``active``
        (each with ``ends_at``) and ``adds_now`` (``{running, sandbox_hours}``)."""
        return self._cloud._request("GET", "/v1/billing/burst") or {}

    def activate(self, quantity: int = 1) -> Dict[str, Any]:
        """Activate ``quantity`` passes (an owner's admin key). Returns
        ``{ok, activated, ends_at, held, running_limit}`` when they were held,
        or a checkout ``{url, quantity, activate_on_payment, usd}`` for the
        rest, activated once paid. 404 ``not_available`` when none is sold
        here, 409 ``burst_cap`` past ``max_active``, 402 ``plan_required``
        without a plan."""
        return self._cloud._request("POST", "/v1/billing/burst", _burst_body(quantity)) or {}


class AsyncCloudBurst:
    """:class:`CloudBurst` for :class:`~kosa8.AsyncCloud`: every call is a coroutine."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    async def get(self) -> Dict[str, Any]:
        return await self._cloud._request("GET", "/v1/billing/burst") or {}

    async def activate(self, quantity: int = 1) -> Dict[str, Any]:
        return await self._cloud._request("POST", "/v1/billing/burst", _burst_body(quantity)) or {}


# ---------------------------------------------------------------------------
# Capability tokens


class CloudTokens:
    """Capability tokens (``cloud.tokens``): hand an agent or a browser a
    credential (``k8c_tok_…``) that reaches only some sandboxes, for at most a
    day. A limited token answers 403 ``token_restricted`` on every other
    route. A token cannot make tokens."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def create(
        self,
        scopes: Sequence[str],
        *,
        sandbox_ids: Optional[Sequence[str]] = None,
        labels: Optional[Mapping[str, str]] = None,
        ttl_s: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make one; its ``secret`` is shown only in this answer (pass it as
        ``api_key`` to a :class:`~kosa8.Kosa8Cloud`). ``scopes`` are some of
        ``sandboxes:read``, ``sandboxes:write``, ``functions:invoke`` (only
        scopes the maker holds); ``sandbox_ids`` up to 100; ``labels`` every
        sandbox it reaches must carry (a sandbox it creates gets them);
        ``ttl_s`` 60 to 86400 (default 3600)."""
        body = _token_body(scopes, sandbox_ids, labels, ttl_s, name)
        return (self._cloud._request("POST", "/v1/tokens", body) or {}).get("token", {})

    def list(self, *, all: bool = False) -> List[Dict[str, Any]]:  # noqa: A002,A003
        """Tokens (never secrets); with ``all``, the last day's revoked and expired ones too."""
        return list((self._cloud._request("GET", "/v1/tokens?all=1" if all else "/v1/tokens") or {}).get("tokens", []))

    def revoke(self, token_id: str) -> Dict[str, Any]:
        """Revoke one; it stops at once. Returns the token."""
        return (self._cloud._request("DELETE", f"/v1/tokens/{_q(token_id)}") or {}).get("token", {})


class AsyncCloudTokens:
    """:class:`CloudTokens` for :class:`~kosa8.AsyncCloud`: every call is a coroutine."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    async def create(
        self,
        scopes: Sequence[str],
        *,
        sandbox_ids: Optional[Sequence[str]] = None,
        labels: Optional[Mapping[str, str]] = None,
        ttl_s: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = _token_body(scopes, sandbox_ids, labels, ttl_s, name)
        return (await self._cloud._request("POST", "/v1/tokens", body) or {}).get("token", {})

    async def list(self, *, all: bool = False) -> List[Dict[str, Any]]:  # noqa: A002,A003
        return list((await self._cloud._request("GET", "/v1/tokens?all=1" if all else "/v1/tokens") or {}).get("tokens", []))

    async def revoke(self, token_id: str) -> Dict[str, Any]:
        return (await self._cloud._request("DELETE", f"/v1/tokens/{_q(token_id)}") or {}).get("token", {})


# ---------------------------------------------------------------------------
# Recordings


class CloudRecordings:
    """The org's desktop session recordings (``cloud.recordings``): an MP4
    made when a sandbox created with ``record=True`` is deleted. One
    sandbox's are also ``box.recordings()``."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def list(self, *, sandbox_id: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:  # noqa: A003
        """Newest first: ``{recordings, configured, retention_days}``;
        ``sandbox_id`` for one sandbox's, ``limit`` 1 to 200. ``configured``
        says whether recordings are set up on this kosa8 Cloud."""
        return _recordings_of(self._cloud._request("GET", _recordings_path(sandbox_id, limit)))

    def link(self, recording_id: str, *, ttl_s: Optional[int] = None) -> Dict[str, Any]:
        """A signed download URL for its MP4: ``{url, expires_at, bytes}``
        (``ttl_s`` 60 to 86400, default 900)."""
        r = self._cloud._request("POST", f"/v1/recordings/{_q(recording_id)}/link", _link_body(ttl_s)) or {}
        return r.get("link", {})

    def delete(self, recording_id: str) -> None:
        """Delete it now."""
        self._cloud._request("DELETE", f"/v1/recordings/{_q(recording_id)}")


class AsyncCloudRecordings:
    """:class:`CloudRecordings` for :class:`~kosa8.AsyncCloud`: every call is a coroutine."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    async def list(self, *, sandbox_id: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:  # noqa: A003
        return _recordings_of(await self._cloud._request("GET", _recordings_path(sandbox_id, limit)))

    async def link(self, recording_id: str, *, ttl_s: Optional[int] = None) -> Dict[str, Any]:
        r = await self._cloud._request("POST", f"/v1/recordings/{_q(recording_id)}/link", _link_body(ttl_s)) or {}
        return r.get("link", {})

    async def delete(self, recording_id: str) -> None:
        await self._cloud._request("DELETE", f"/v1/recordings/{_q(recording_id)}")


__all__ = [
    "CloudBurst",
    "AsyncCloudBurst",
    "CloudTokens",
    "AsyncCloudTokens",
    "CloudRecordings",
    "AsyncCloudRecordings",
    "TOKEN_SCOPES",
]
