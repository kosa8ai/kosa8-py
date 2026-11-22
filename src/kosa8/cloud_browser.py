# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 Cloud browsers (headless Chromium driven over CDP) and the AI gateway.
:mod:`kosa8.cloud` wires these onto :class:`~kosa8.Kosa8Cloud` as
``cloud.browsers`` and ``cloud.ai``. Answers are the API's own dicts.

>>> with cloud.browser(ttl_s=120) as b:
...     png = b.screenshot("https://example.com")
...     # or drive it: playwright.chromium.connect_over_cdp(b.cdp_url)

A browser costs $0.12 per browser-hour, billed per second.

The async twins (``AsyncCloudBrowsers``, ``AsyncCloudBrowser``,
``AsyncCloudAI``) are on :class:`~kosa8.AsyncCloud`; every call is a coroutine.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional

from ._calls import AsyncCalls, Calls, as_dict, key

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


#: The CDP WebSocket of a browser. It needs the short-lived token the API puts
#: in ``cdp_url`` (``wss://.../v1/browsers/{id}/cdp?token=...``), so connect with
#: :attr:`CloudBrowser.cdp_url`, not this path alone.
CDP_PATH = "/v1/browsers/{id}/cdp"


def cdp_path(browser_id: str) -> str:
    """The path of a browser's CDP WebSocket (without its token)."""
    return f"/v1/browsers/{_q(browser_id)}/cdp"


def _page_body(url: str, text: Optional[bool], screenshot: Optional[bool], full_page: Optional[bool]) -> Dict[str, Any]:
    if not isinstance(url, str) or not url:
        raise ValueError("url is required")
    body: Dict[str, Any] = {"url": url}
    for k, v in (("screenshot", screenshot), ("text", text), ("full_page", full_page)):
        if v is not None:
            body[k] = v
    return body


def _png_of(p: Mapping[str, Any]) -> bytes:
    b64 = p.get("screenshot_png_base64")
    if not b64:
        raise ValueError("the API sent no screenshot")
    return base64.b64decode(b64)


class CloudBrowser(Calls):
    """One cloud browser. ``info`` is its state as of the last call; ``cdp_url``
    (valid 10 minutes) connects Playwright or Puppeteer to it:
    ``chromium.connect_over_cdp(b.cdp_url)``. Call :meth:`refresh` for a fresh one.

    ``with cloud.browser() as b:`` closes it on the way out, error or not.
    """

    def __init__(self, cloud: Any, info: Dict[str, Any]) -> None:
        super().__init__(cloud)
        self.info = info

    def __repr__(self) -> str:  # never the cdp_url (it carries a token)
        return f"{type(self).__name__}(id={self.id!r}, state={self.state!r})"

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
        """``wss://.../v1/browsers/{id}/cdp?token=...`` for ``connect_over_cdp``
        while open (``None`` when closed or in a list answer)."""
        return self.info.get("cdp_url")

    @property
    def cdp_url_expires_at(self) -> Any:
        return self.info.get("cdp_url_expires_at")

    def _set(self, answer: Any) -> "CloudBrowser":
        self.info = answer["browser"]
        return self

    def refresh(self) -> "CloudBrowser":
        """Read it again: its state and, while open, a fresh ``cdp_url``."""
        return self._call("GET", self._path, then=self._set)

    def page(self, url: str, *, text: Optional[bool] = None, screenshot: Optional[bool] = None,
             full_page: Optional[bool] = None) -> Dict[str, Any]:
        """Load ``url`` in it; the ``page`` dict: ``url``, ``title`` and, if
        asked, ``text`` and ``screenshot_png_base64``."""
        return self._call("POST", f"{self._path}/page", _page_body(url, text, screenshot, full_page),
                          then=key("page", dict))

    def goto(self, url: str, *, text: bool = True) -> Dict[str, Any]:
        """Load ``url``; returns ``url``, ``title`` and (unless ``text=False``) the page's ``text``."""
        return self.page(url, text=text)

    def screenshot(self, url: str, *, full_page: bool = False) -> bytes:
        """Load ``url`` and return a PNG of it (the viewport, or the whole page with ``full_page``)."""
        return self._call("POST", f"{self._path}/page", _page_body(url, None, True, full_page or None),
                          then=lambda r: _png_of(as_dict(r).get("page") or {}))

    def _closed(self, answer: Any) -> Dict[str, Any]:
        self.info = {**self.info, "state": "closed", "cdp_url": None}
        return as_dict(answer)

    def close(self) -> Dict[str, Any]:
        """Close it; billing stops."""
        return self._call("DELETE", self._path, then=self._closed)


class AsyncCloudBrowser(AsyncCalls, CloudBrowser):
    """:class:`CloudBrowser` for :class:`~kosa8.AsyncCloud`: every call is a
    coroutine; ``async with await cloud.browser() as b:`` closes it."""

    async def __aenter__(self) -> "AsyncCloudBrowser":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "CloudBrowser":
        raise TypeError("use `async with` on an AsyncCloudBrowser")


class CloudBrowsers(Calls):
    """The org's cloud browsers: headless Chromium on demand, driven over CDP.
    $0.12 per browser-hour, billed per second."""

    _browser = CloudBrowser

    def _one(self, answer: Any) -> CloudBrowser:
        return self._browser(self._cloud, answer["browser"])

    def create(self, *, ttl_s: Optional[int] = None, sandbox_id: Optional[str] = None,
               name: Optional[str] = None) -> CloudBrowser:
        """Open a browser. ``ttl_s`` (30 to 3600, default 300) closes it on its
        own; ``sandbox_id`` ties it to a sandbox; ``name`` is a label."""
        _check_ttl(ttl_s)
        body: Dict[str, Any] = {}
        for k, v in (("ttl_s", ttl_s), ("sandbox_id", sandbox_id), ("name", name)):
            if v is not None:
                body[k] = v
        return self._call("POST", "/v1/browsers", body, then=self._one)

    @staticmethod
    def _list_path(state: Optional[str]) -> str:
        if state is not None and state not in ("open", "all"):
            raise ValueError('state must be "open" or "all"')
        return "/v1/browsers" + (f"?state={_q(state)}" if state else "")

    def list(self, *, state: Optional[str] = None) -> List[CloudBrowser]:  # noqa: A003
        """Open browsers (``state="all"`` for closed ones too). List items have no ``cdp_url``: :meth:`CloudBrowser.refresh`."""
        return self._call("GET", self._list_path(state),
                          then=lambda r: [self._browser(self._cloud, b) for b in as_dict(r).get("browsers", [])])

    def info(self, *, state: Optional[str] = None) -> Dict[str, Any]:
        """The list answer as sent: ``browsers``, ``enabled``, ``limit``, ``price_per_hour_usd``."""
        return self._call("GET", self._list_path(state), then=as_dict)

    def get(self, browser_id: str) -> CloudBrowser:
        """One browser, with a fresh ``cdp_url`` while open."""
        return self._call("GET", f"/v1/browsers/{_q(browser_id)}", then=self._one)

    def cdp_url(self, browser_id: str) -> Optional[str]:
        """A fresh ``wss://`` CDP URL (with its token) for an open browser, or ``None`` once closed."""
        return self._call("GET", f"/v1/browsers/{_q(browser_id)}", then=lambda r: as_dict(as_dict(r).get("browser")).get("cdp_url"))

    def delete(self, browser_id: str) -> Dict[str, Any]:
        """Close a browser by id."""
        return self._call("DELETE", f"/v1/browsers/{_q(browser_id)}", then=as_dict)


class AsyncCloudBrowsers(AsyncCalls, CloudBrowsers):
    """:class:`CloudBrowsers` for :class:`~kosa8.AsyncCloud`."""

    _browser = AsyncCloudBrowser


#: Providers the AI gateway routes for (``GET /v1/ai/settings`` ``providers`` is the live list).
AI_PROVIDERS = ("openai", "anthropic", "groq", "mistral", "deepseek", "openrouter", "google-ai-studio", "workers-ai")


class CloudAI(Calls):
    """The org's AI gateway: provider keys kept server-side, caching, rate
    limits and usage. A sandbox made with ``ai_gateway=True`` calls models
    through it (``OPENAI_BASE_URL`` etc. are set in its env). Outside a
    sandbox, point a provider SDK at :meth:`gateway_url` with your kosa8 API
    key as its key::

        OpenAI(base_url=cloud.ai.gateway_url("openai"), api_key=os.environ["KOSA8_API_KEY"])
        Anthropic(base_url=cloud.ai.gateway_url("anthropic"), api_key=os.environ["KOSA8_API_KEY"])
    """

    def settings(self) -> Dict[str, Any]:
        """``settings`` (``enabled``, ``all_sandboxes``, ``cache_ttl_s``,
        ``rate_per_min``), ``available``, ``base_url``, ``providers``, ``configured``..."""
        return self._call("GET", "/v1/ai/settings", then=as_dict)

    def update_settings(self, *, enabled: Optional[bool] = None, all_sandboxes: Optional[bool] = None,
                        cache_ttl_s: Optional[int] = None, rate_per_min: Optional[int] = None) -> Dict[str, Any]:
        """Change the given settings only."""
        body = {k: v for k, v in (("enabled", enabled), ("all_sandboxes", all_sandboxes),
                                  ("cache_ttl_s", cache_ttl_s), ("rate_per_min", rate_per_min)) if v is not None}
        if not body:
            raise ValueError("give at least one setting to change")
        return self._call("PATCH", "/v1/ai/settings", body, then=as_dict)

    def set_key(self, provider: str, key: str) -> Dict[str, Any]:
        """Store a provider's API key (``openai``, ``anthropic``...). It is never sent back."""
        if not provider or not key:
            raise ValueError("provider and key are required")
        return self._call("PUT", f"/v1/ai/keys/{_q(provider)}", {"key": key}, then=as_dict)

    def keys(self) -> List[Dict[str, Any]]:
        """Stored keys: ``provider``, ``hint``, ``created_at``, ``updated_at`` (never the key)."""
        return self._call("GET", "/v1/ai/keys", then=lambda r: as_dict(r).get("keys", []))

    def delete_key(self, provider: str) -> Dict[str, Any]:
        if not provider:
            raise ValueError("provider is required")
        return self._call("DELETE", f"/v1/ai/keys/{_q(provider)}", then=as_dict)

    def usage(self, month: Optional[str] = None) -> Dict[str, Any]:
        """A month's (``YYYY-MM``, default this one) calls and cost: ``total``,
        ``by_model``, ``by_sandbox``, ``recent``."""
        if month is not None and not _MONTH.match(month):
            raise ValueError(f"month must be YYYY-MM, got {month!r}")
        return self._call("GET", "/v1/ai/usage" + (f"?month={_q(month)}" if month else ""), then=as_dict)

    @staticmethod
    def _gw_path(provider: str, path: str = "") -> str:
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider is required, e.g. openai or anthropic")
        path = path.lstrip("/")
        if ".." in path:
            raise ValueError("not a provider API path")
        return f"/v1/ai/gw/{_q(provider)}/{path}"

    def gateway_url(self, provider: str, path: str = "") -> str:
        """The gateway's base URL for a provider (``https://api.kosa8.com/v1/ai/gw/openai/``):
        give it to the provider's SDK as its base URL, with a kosa8 API key as
        its key. The org's own provider key is attached server-side."""
        return self._cloud.base_url + self._gw_path(provider, path)

    def call(self, provider: str, path: str, body: Mapping[str, Any]) -> Any:
        """One model call through the gateway in the provider's own request
        shape, e.g. ``cloud.ai.call("openai", "chat/completions", {...})``.
        Billed by the provider to the org's key; not retried (it may have run)."""
        if not path:
            raise ValueError("path is required, e.g. chat/completions")
        return self._call("POST", self._gw_path(provider, path), dict(body))


class AsyncCloudAI(AsyncCalls, CloudAI):
    """:class:`CloudAI` for :class:`~kosa8.AsyncCloud`."""
