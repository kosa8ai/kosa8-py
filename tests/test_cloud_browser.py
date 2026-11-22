# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
import base64
import unittest

from kosa8 import BROWSER_PRICE_PER_HOUR_USD, CloudBrowser, Kosa8ApiError

from test_cloud_features import make, route, sbx

CDP = "wss://api.kosa8.com/v1/browsers/brw_1/cdp?token=t0k"


def brw(**over):
    d = {"id": "brw_1", "name": None, "sandbox_id": None, "state": "open", "ttl_s": 300,
         "created_at": 1, "expires_at": 2, "closed_at": None, "close_reason": None, "seconds": 0,
         "cdp_url": CDP, "cdp_url_expires_at": 3}
    d.update(over)
    return d


PNG = b"\x89PNG\r\n\x1a\nfake"


def browser_api(s):
    r = route(s)
    if r == "POST /v1/browsers":
        return 201, {"browser": brw(**{k: v for k, v in (s["body"] or {}).items()}), "price_per_hour_usd": 0.12}
    if r == "GET /v1/browsers":
        return 200, {"browsers": [{k: v for k, v in brw().items() if k != "cdp_url"}], "enabled": True,
                     "limit": 5, "price_per_hour_usd": 0.12}
    if r == "GET /v1/browsers/brw_1":
        return 200, {"browser": brw(cdp_url=CDP + "2")}
    if r == "DELETE /v1/browsers/brw_1":
        return 200, {"ok": True, "id": "brw_1"}
    if r == "POST /v1/browsers/brw_1/page":
        b = s["body"]
        page = {"url": b["url"], "title": "Example Domain"}
        if b.get("text"):
            page["text"] = "Example Domain"
        if b.get("screenshot"):
            page["screenshot_png_base64"] = base64.b64encode(PNG).decode()
        return 200, {"browser_id": "brw_1", "page": page}
    if r == "GET /v1/browsers/brw_gone":
        return 410, {"error": "browser_closed", "message": "browser is closed"}
    return 404, {"error": "not_found"}


class Browsers(unittest.TestCase):
    def test_create_sends_options_and_returns_browser(self):
        cloud, stub = make(browser_api)
        b = cloud.browser(ttl_s=60, sandbox_id="sbx_1", name="scrape")
        self.assertIsInstance(b, CloudBrowser)
        self.assertEqual(stub.seen[0]["body"], {"ttl_s": 60, "sandbox_id": "sbx_1", "name": "scrape"})
        self.assertEqual(b.id, "brw_1")
        self.assertEqual(b.cdp_url, CDP)
        self.assertNotIn("t0k", repr(b))

    def test_create_defaults_to_empty_body(self):
        cloud, stub = make(browser_api)
        cloud.browsers.create()
        self.assertEqual(stub.seen[0]["body"], {})

    def test_ttl_is_checked(self):
        cloud, stub = make(browser_api)
        for bad in (10, 4000, 1.5, True):
            with self.assertRaises(ValueError):
                cloud.browser(ttl_s=bad)
        self.assertEqual(stub.seen, [])

    def test_refresh_gets_fresh_cdp_url(self):
        cloud, _ = make(browser_api)
        b = cloud.browser()
        self.assertIs(b.refresh(), b)
        self.assertEqual(b.cdp_url, CDP + "2")

    def test_goto_returns_text(self):
        cloud, stub = make(browser_api)
        p = cloud.browser().goto("https://example.com")
        self.assertEqual(stub.seen[-1]["body"], {"url": "https://example.com", "text": True})
        self.assertEqual(p["title"], "Example Domain")
        self.assertEqual(p["text"], "Example Domain")

    def test_screenshot_decodes_png(self):
        cloud, stub = make(browser_api)
        b = cloud.browser()
        self.assertEqual(b.screenshot("https://example.com"), PNG)
        self.assertEqual(stub.seen[-1]["body"], {"url": "https://example.com", "screenshot": True})
        b.screenshot("https://example.com", full_page=True)
        self.assertEqual(stub.seen[-1]["body"], {"url": "https://example.com", "screenshot": True, "full_page": True})

    def test_context_manager_closes(self):
        cloud, stub = make(browser_api)
        with cloud.browser() as b:
            pass
        self.assertEqual(route(stub.seen[-1]), "DELETE /v1/browsers/brw_1")
        self.assertEqual(b.state, "closed")
        self.assertIsNone(b.cdp_url)

    def test_context_manager_closes_on_error(self):
        cloud, stub = make(browser_api)
        with self.assertRaises(RuntimeError):
            with cloud.browser():
                raise RuntimeError("boom")
        self.assertEqual(route(stub.seen[-1]), "DELETE /v1/browsers/brw_1")

    def test_list_get_delete(self):
        cloud, stub = make(browser_api)
        bs = cloud.list_browsers(state="all")
        self.assertEqual(stub.seen[-1]["query"], {"state": ["all"]})
        self.assertEqual([b.id for b in bs], ["brw_1"])
        self.assertIsNone(bs[0].cdp_url)
        self.assertEqual(cloud.browsers.info()["limit"], 5)
        self.assertEqual(cloud.get_browser("brw_1").cdp_url, CDP + "2")
        self.assertEqual(cloud.browsers.delete("brw_1"), {"ok": True, "id": "brw_1"})
        with self.assertRaises(ValueError):
            cloud.list_browsers(state="closed")

    def test_closed_browser_raises_api_error(self):
        cloud, _ = make(browser_api)
        with self.assertRaises(Kosa8ApiError) as cm:
            cloud.get_browser("brw_gone")
        self.assertEqual(cm.exception.status, 410)

    def test_price_constant(self):
        self.assertEqual(BROWSER_PRICE_PER_HOUR_USD, 0.12)


def ai_api(s):
    r = route(s)
    if r == "GET /v1/ai/settings":
        return 200, {"settings": {"enabled": True, "all_sandboxes": False, "cache_ttl_s": 0, "rate_per_min": 60},
                     "available": True, "base_url": "https://gw", "gateway_id": "g", "providers": ["openai"],
                     "workers_ai": True, "configured": True}
    if r == "PATCH /v1/ai/settings":
        return 200, {"settings": s["body"]}
    if r == "PUT /v1/ai/keys/openai":
        return 200, {"ok": True, "provider": "openai", "hint": "sk-...abcd"}
    if r == "GET /v1/ai/keys":
        return 200, {"keys": [{"provider": "openai", "hint": "sk-...abcd", "created_at": 1, "updated_at": 1}],
                     "providers": ["openai", "anthropic"]}
    if r == "DELETE /v1/ai/keys/openai":
        return 200, {"ok": True}
    if r == "GET /v1/ai/usage":
        return 200, {"month": (s["query"].get("month") or ["2026-11"])[0], "total": {"calls": 3}}
    if r == "POST /v1/sandboxes":
        return 201, {"sandbox": sbx()}
    return 404, {"error": "not_found"}


class AIGateway(unittest.TestCase):
    def test_settings(self):
        cloud, stub = make(ai_api)
        self.assertTrue(cloud.ai_settings()["settings"]["enabled"])
        cloud.update_ai_settings(enabled=True, rate_per_min=120)
        self.assertEqual(stub.seen[-1]["body"], {"enabled": True, "rate_per_min": 120})
        with self.assertRaises(ValueError):
            cloud.ai.update_settings()

    def test_keys(self):
        cloud, stub = make(ai_api)
        cloud.set_ai_key("openai", "sk-secret")
        self.assertEqual(stub.seen[-1]["body"], {"key": "sk-secret"})
        self.assertEqual(cloud.list_ai_keys()[0]["provider"], "openai")
        self.assertEqual(cloud.delete_ai_key("openai"), {"ok": True})
        self.assertEqual(route(stub.seen[-1]), "DELETE /v1/ai/keys/openai")
        with self.assertRaises(ValueError):
            cloud.set_ai_key("openai", "")

    def test_usage(self):
        cloud, stub = make(ai_api)
        self.assertEqual(cloud.ai_usage()["total"]["calls"], 3)
        self.assertEqual(stub.seen[-1]["query"], {})
        self.assertEqual(cloud.ai_usage("2026-10")["month"], "2026-10")
        with self.assertRaises(ValueError):
            cloud.ai_usage("2026-13")

    def test_sandbox_ai_gateway_option(self):
        cloud, stub = make(ai_api)
        cloud.sandboxes.create(ai_gateway=True)
        self.assertEqual(stub.seen[-1]["body"], {"ai_gateway": True})
        cloud.sandboxes.create(ai_gateway=False)
        self.assertEqual(stub.seen[-1]["body"], {"ai_gateway": False})
        cloud.sandboxes.create()
        self.assertNotIn("ai_gateway", stub.seen[-1]["body"] or {})


class AsyncAIGatewayOption(unittest.TestCase):
    def test_async_create_sends_ai_gateway(self):
        import inspect

        from kosa8.aio import AsyncCloudSandboxes

        self.assertIn("ai_gateway", inspect.signature(AsyncCloudSandboxes.create).parameters)


if __name__ == "__main__":
    unittest.main()
