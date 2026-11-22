# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 Cloud browsers (headless Chromium driven over CDP) and the AI gateway.
:mod:`kosa8.cloud` wires these onto :class:`~kosa8.Kosa8Cloud` as
``cloud.browsers`` and ``cloud.ai``. Answers are the API's own dicts.

>>> with cloud.browser(ttl_s=120) as b:
...     png = b.screenshot("https://example.com")
...     # or drive it: playwright.chromium.connect_over_cdp(b.cdp_url)

A browser costs $0.12 per browser-hour, billed per second.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Dict, List, Optional

#: USD per browser-hour, billed per second (the API's ``price_per_hour_usd`` is the live figure).
BROWSER_PRICE_PER_HOUR_USD = 0.12

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


def _check_ttl(ttl_s: Optional[int]) -> None:
    if ttl_s is None:
        return
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or not 30 <= ttl_s <= 3600:
        raise ValueError(f"ttl_s must be a whole number from 30 to 3600, got {ttl_s!r}")


class CloudBrowser:
    """One cloud browser. ``info`` is its state as of the last call; ``cdp_url``
    (valid 10 minutes) connects Playwright or Puppeteer to it:
    ``chromium.connect_over_cdp(b.cdp_url)``. Call :meth:`refresh` for a fresh one.

    ``with cloud.browser() as b:`` closes it on the way out, error or not.
    """

    def __init__(self, cloud: Any, info: Dict[str, Any]) -> None:
        self._cloud = cloud
        self.info = info

    def __repr__(self) -> str:  # never the cdp_url (it carries a token)
        return f"CloudBrowser(id={self.id!r}, state={self.state!r})"

    def __enter__(self) -> "CloudBrowser":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - closing on the way out must not hide the real error
            pass

    @property
    def _path(self) -> str:
        return f"/v1/browsers/{_q(self.id)}"

    @property
    def id(self) -> str:
        return self.info["id"]

    @property
    def state(self) -> str:
        return self.info.get("state", "")

    @property
    def cdp_url(self) -> Optional[str]:
        """``wss://...`` for ``connect_over_cdp`` while open (``None`` when closed or in a list answer)."""
        return self.info.get("cdp_url")

    @property
    def cdp_url_expires_at(self) -> Any:
        return self.info.get("cdp_url_expires_at")

    def refresh(self) -> "CloudBrowser":
        """Read it again: its state and, while open, a fresh ``cdp_url``."""
        self.info = self._cloud._request("GET", self._path)["browser"]
        return self

    def page(self, url: str, *, text: Optional[bool] = None, screenshot: Optional[bool] = None,
             full_page: Optional[bool] = None) -> Dict[str, Any]:
        """Load ``url`` in it; the ``page`` dict: ``url``, ``title`` and, if
        asked, ``text`` and ``screenshot_png_base64``."""
        if not isinstance(url, str) or not url:
            raise ValueError("url is required")
        body: Dict[str, Any] = {"url": url}
        for k, v in (("screenshot", screenshot), ("text", text), ("full_page", full_page)):
            if v is not None:
                body[k] = v
        return self._cloud._request("POST", f"{self._path}/page", body)["page"]

    def goto(self, url: str, *, text: bool = True) -> Dict[str, Any]:
        """Load ``url``; returns ``url``, ``title`` and (unless ``text=False``) the page's ``text``."""
        return self.page(url, text=text)

    def screenshot(self, url: str, *, full_page: bool = False) -> bytes:
        """Load ``url`` and return a PNG of it (the viewport, or the whole page with ``full_page``)."""
        p = self.page(url, screenshot=True, full_page=full_page or None)
        b64 = p.get("screenshot_png_base64")
        if not b64:
            raise ValueError("the API sent no screenshot")
        return base64.b64decode(b64)

    def close(self) -> Dict[str, Any]:
        """Close it; billing stops."""
        r = self._cloud._request("DELETE", self._path) or {}
        self.info = {**self.info, "state": "closed", "cdp_url": None}
        return r


class CloudBrowsers:
    """The org's cloud browsers: headless Chromium on demand, driven over CDP.
    $0.12 per browser-hour, billed per second."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def create(self, *, ttl_s: Optional[int] = None, sandbox_id: Optional[str] = None,
               name: Optional[str] = None) -> CloudBrowser:
        """Open a browser. ``ttl_s`` (30 to 3600, default 300) closes it on its
        own; ``sandbox_id`` ties it to a sandbox; ``name`` is a label."""
        _check_ttl(ttl_s)
        body: Dict[str, Any] = {}
        for k, v in (("ttl_s", ttl_s), ("sandbox_id", sandbox_id), ("name", name)):
            if v is not None:
                body[k] = v
        r = self._cloud._request("POST", "/v1/browsers", body)
        return CloudBrowser(self._cloud, r["browser"])

    def list(self, *, state: Optional[str] = None) -> List[CloudBrowser]:  # noqa: A003
        """Open browsers (``state="all"`` for closed ones too). List items have no ``cdp_url``: :meth:`CloudBrowser.refresh`."""
        if state is not None and state not in ("open", "all"):
            raise ValueError('state must be "open" or "all"')
        path = "/v1/browsers" + (f"?state={_q(state)}" if state else "")
        return [CloudBrowser(self._cloud, b) for b in (self._cloud._request("GET", path) or {}).get("browsers", [])]

    def info(self, *, state: Optional[str] = None) -> Dict[str, Any]:
        """The list answer as sent: ``browsers``, ``enabled``, ``limit``, ``price_per_hour_usd``."""
        path = "/v1/browsers" + (f"?state={_q(state)}" if state else "")
        return self._cloud._request("GET", path) or {}

    def get(self, browser_id: str) -> CloudBrowser:
        """One browser, with a fresh ``cdp_url`` while open."""
        return CloudBrowser(self._cloud, self._cloud._request("GET", f"/v1/browsers/{_q(browser_id)}")["browser"])

    def delete(self, browser_id: str) -> Dict[str, Any]:
        """Close a browser by id."""
        return self._cloud._request("DELETE", f"/v1/browsers/{_q(browser_id)}") or {}


class CloudAI:
    """The org's AI gateway: provider keys kept server-side, caching, rate
    limits and usage. A sandbox made with ``ai_gateway=True`` calls models
    through it (``OPENAI_BASE_URL`` etc. are set in its env)."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def settings(self) -> Dict[str, Any]:
        """``settings`` (``enabled``, ``all_sandboxes``, ``cache_ttl_s``,
        ``rate_per_min``), ``available``, ``base_url``, ``providers``, ``configured``..."""
        return self._cloud._request("GET", "/v1/ai/settings") or {}

    def update_settings(self, *, enabled: Optional[bool] = None, all_sandboxes: Optional[bool] = None,
                        cache_ttl_s: Optional[int] = None, rate_per_min: Optional[int] = None) -> Dict[str, Any]:
        """Change the given settings only."""
        body = {k: v for k, v in (("enabled", enabled), ("all_sandboxes", all_sandboxes),
                                  ("cache_ttl_s", cache_ttl_s), ("rate_per_min", rate_per_min)) if v is not None}
        if not body:
            raise ValueError("give at least one setting to change")
        return self._cloud._request("PATCH", "/v1/ai/settings", body) or {}

    def set_key(self, provider: str, key: str) -> Dict[str, Any]:
        """Store a provider's API key (``openai``, ``anthropic``...). It is never sent back."""
        if not provider or not key:
            raise ValueError("provider and key are required")
        return self._cloud._request("PUT", f"/v1/ai/keys/{_q(provider)}", {"key": key}) or {}

    def keys(self) -> List[Dict[str, Any]]:
        """Stored keys: ``provider``, ``hint``, ``created_at``, ``updated_at`` (never the key)."""
        return (self._cloud._request("GET", "/v1/ai/keys") or {}).get("keys", [])

    def delete_key(self, provider: str) -> Dict[str, Any]:
        return self._cloud._request("DELETE", f"/v1/ai/keys/{_q(provider)}") or {}

    def usage(self, month: Optional[str] = None) -> Dict[str, Any]:
        """A month's (``YYYY-MM``, default this one) calls and cost: ``total``,
        ``by_model``, ``by_sandbox``, ``recent``."""
        if month is not None and not _MONTH.match(month):
            raise ValueError(f"month must be YYYY-MM, got {month!r}")
        return self._cloud._request("GET", "/v1/ai/usage" + (f"?month={_q(month)}" if month else "")) or {}
