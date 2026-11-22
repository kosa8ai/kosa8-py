# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""0.6.0: account calls, API keys, webhooks, audit, benchmarks, RL compare and
alerts, function invocations, background exec stop, item deletes, small
sizes, overflow refusals, the AI gateway URL and the CDP path, on both
clients; and the retries on database_busy, template_building and 429."""

import asyncio
import json
import unittest
import urllib.parse
from unittest import mock

import kosa8
from kosa8 import AsyncCloud, CLOUD_SIZES, Kosa8Cloud, Kosa8UnsupportedError, RetryPolicy, classify_failure
from kosa8.errors import Kosa8ApiError

KEY = "k8c_live_testkey"


class Stub:
    """A fake transport: ``answer(seen)`` gives (status, payload[, headers])."""

    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        raw = headers.get("Content-Type") == "application/octet-stream"
        s = {"method": method, "path": u.path, "query": urllib.parse.parse_qs(u.query), "headers": dict(headers),
             "body": body if raw else (json.loads(body) if body else None)}
        self.seen.append(s)
        out = self.answer(s)
        status, payload = out[0], out[1]
        hdrs = out[2] if len(out) > 2 else {}
        data = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        return status, data, hdrs

    async def aopen(self, *a):
        return self(*a)


def both(answer):
    """A sync and an async client on one stub."""
    stub = Stub(answer)
    return Kosa8Cloud(KEY, env={}, opener=stub), AsyncCloud(KEY, env={}, async_opener=stub.aopen), stub


def run(coro):
    return asyncio.run(coro)


def route(s):
    return f"{s['method']} {s['path']}"


ANSWERS = {
    "GET /v1/pricing": {"rates": {"vcpu_h": 0.048, "browser_h": 0.12}},
    "GET /v1/limits": {"active": 1, "max_running": 10},
    "POST /v1/limits/request": {"requested": {"max_running": 50, "from": 10}},
    "GET /v1/usage": {"month": "2026-11"},
    "GET /v1/usage/insights": {"daily": []},
    "GET /v1/images": {"images": [{"template": "python", "digest": "sha256:x"}]},
    "GET /v1/lineage": {"nodes": [], "roots": [], "forked": 0},
    "GET /v1/checkpoints": {"sandboxes": []},
    "GET /v1/api-keys": {"api_keys": [{"id": "key_1"}]},
    "POST /v1/api-keys": {"api_key": {"id": "key_2", "secret": "k8c_live_new"}, "note": "once"},
    "DELETE /v1/api-keys/key_1": {"ok": True, "id": "key_1"},
    "POST /v1/api-keys/key_1/rotate": {"api_key": {"id": "key_3"}, "old_key": {"id": "key_1"}},
    "GET /v1/webhooks": {"webhooks": [{"id": "wh_1"}], "events": ["sandbox.created"]},
    "POST /v1/webhooks": {"webhook": {"id": "wh_2", "secret": "whsec_x"}, "note": "once"},
    "DELETE /v1/webhooks/wh_1": {"ok": True, "id": "wh_1"},
    "GET /v1/webhooks/wh_1/deliveries": {"deliveries": [{"id": "dl_1"}]},
    "POST /v1/webhooks/wh_1/test": {"delivery": {"id": "dl_2"}},
    "GET /v1/audit": {"events": [{"seq": 2}], "next_before_seq": 2},
    "GET /v1/audit/verify": {"ok": True, "count": 2},
    "GET /v1/benchmarks": {"benchmarks": [{"name": "swe-bench-verified"}]},
    "GET /v1/benchmarks/swe": {"benchmark": {"name": "swe"}},
    "GET /v1/benchmarks/swe/instances": {"instances": [{"id": "a"}], "next_cursor": None},
    "GET /v1/benchmarks/swe/instances/a": {"instance": {"id": "a", "state": "ready"}},
    "POST /v1/benchmarks/swe/prepare": {"queued": 3},
    "GET /v1/rl/runs": {"runs": [], "next_cursor": None},
    "GET /v1/rl/runs/run_1": {"run": {"id": "run_1"}},
    "GET /v1/rl/compare": {"runs": [{"run": {"id": "run_1"}}, {"run": {"id": "run_2"}}]},
    "GET /v1/rl/alerts": {"alerts": [], "kinds": {"infra_rate": {}}},
    "POST /v1/rl/alerts": {"alert": {"id": "rla_1"}},
    "PATCH /v1/rl/alerts/rla_1": {"alert": {"id": "rla_1", "enabled": False}},
    "DELETE /v1/rl/alerts/rla_1": {"deleted": True, "id": "rla_1"},
    "GET /v1/functions/fn/invocations": {"invocations": [{"id": "inv_1"}]},
    "GET /v1/ai/usage": {"total": {}},
    "GET /v1/browsers/brw_1": {"browser": {"id": "brw_1", "state": "open", "cdp_url": "wss://x/v1/browsers/brw_1/cdp?token=t"}},
    "POST /v1/browsers": {"browser": {"id": "brw_1", "state": "open", "cdp_url": "wss://x/v1/browsers/brw_1/cdp?token=t"}},
    "DELETE /v1/browsers/brw_1": {"ok": True},
    "GET /v1/sandboxes/sbx_1": {"sandbox": {"id": "sbx_1", "state": "running", "region": "cf-auto"}},
    "POST /v1/sandboxes/sbx_1/exec": {"exec": {"id": "bg_1", "state": "running"}},
    "GET /v1/sandboxes/sbx_1/exec/bg_1": {"exec": {"id": "bg_1", "state": "exited", "exit": 0, "output": "hi\n"}},
    "POST /v1/sandboxes/sbx_1/exec/bg_1/stop": {"exec": {"id": "bg_1", "sandbox_id": "sbx_1", "state": "stopping"}},
    "DELETE /v1/sandboxes/sbx_1/previews/3000": {"ok": True},
    "DELETE /v1/sandboxes/sbx_1/schedules/sch_1": {"ok": True},
    "DELETE /v1/sandboxes/sbx_1/code/contexts/default": {"ok": True},
    "POST /v1/sandboxes/sbx_1/fork": (501, {"error": "not_available", "message": "Fork is not available for this sandbox: it runs on kosa8's overflow capacity (Cloudflare)"}),
}


def api(s):
    if s["path"] == "/v1/audit/export":
        return 200, b'{"seq":1}\n{"seq":2}\n'
    a = ANSWERS.get(route(s))
    if a is None:
        return 404, {"error": "not_found", "message": route(s)}
    return a if isinstance(a, tuple) else (200, a)


class Groups(unittest.TestCase):
    """Every group answers the same, sync and async."""

    def check(self, call, want_route, want=None, *, query=None, body=None):
        sync, aio, stub = both(api)
        got = call(sync)
        self.assertEqual(route(stub.seen[-1]), want_route)
        if query is not None:
            self.assertEqual(stub.seen[-1]["query"], query)
        if body is not None:
            self.assertEqual(stub.seen[-1]["body"], body)
        agot = run(call(aio))
        self.assertEqual(route(stub.seen[-1]), want_route)
        if want is not None:
            self.assertEqual(got, want)
        self.assertEqual(got, agot)
        return got

    def test_account(self):
        self.check(lambda c: c.pricing(), "GET /v1/pricing", {"rates": {"vcpu_h": 0.048, "browser_h": 0.12}})
        self.check(lambda c: c.limits(), "GET /v1/limits", {"active": 1, "max_running": 10})
        self.check(lambda c: c.request_limit(50, reason="RL"), "POST /v1/limits/request",
                   body={"max_running": 50, "reason": "RL"})
        self.check(lambda c: c.usage(), "GET /v1/usage")
        self.check(lambda c: c.usage_insights(), "GET /v1/usage/insights", {"daily": []})
        self.check(lambda c: c.images(), "GET /v1/images", [{"template": "python", "digest": "sha256:x"}])
        self.check(lambda c: c.lineage(), "GET /v1/lineage")
        self.check(lambda c: c.checkpoints(), "GET /v1/checkpoints", {"sandboxes": []})

    def test_api_keys(self):
        self.check(lambda c: c.api_keys.list(), "GET /v1/api-keys", [{"id": "key_1"}])
        r = self.check(lambda c: c.api_keys.create("ci", scopes=["sandboxes:read"], expires_in_s=3600),
                       "POST /v1/api-keys", body={"name": "ci", "scopes": ["sandboxes:read"], "expires_in_s": 3600})
        self.assertEqual(r["api_key"]["secret"], "k8c_live_new")
        self.check(lambda c: c.api_keys.revoke("key_1"), "DELETE /v1/api-keys/key_1")
        self.check(lambda c: c.api_keys.rotate("key_1", grace_s=60), "POST /v1/api-keys/key_1/rotate",
                   body={"grace_s": 60})
        sync, _, _ = both(api)
        with self.assertRaises(ValueError):
            sync.api_keys.create("")

    def test_webhooks(self):
        self.check(lambda c: c.webhooks.list(), "GET /v1/webhooks", [{"id": "wh_1"}])
        self.check(lambda c: c.webhooks.events(), "GET /v1/webhooks", ["sandbox.created"])
        self.check(lambda c: c.webhooks.create("https://me.example/hook", events=["sandbox.created"]),
                   "POST /v1/webhooks", {"id": "wh_2", "secret": "whsec_x"},
                   body={"url": "https://me.example/hook", "events": ["sandbox.created"]})
        self.check(lambda c: c.webhooks.delete("wh_1"), "DELETE /v1/webhooks/wh_1")
        self.check(lambda c: c.webhooks.deliveries("wh_1", limit=5), "GET /v1/webhooks/wh_1/deliveries",
                   [{"id": "dl_1"}], query={"limit": ["5"]})
        self.check(lambda c: c.webhooks.test("wh_1"), "POST /v1/webhooks/wh_1/test", {"id": "dl_2"})

    def test_audit(self):
        self.check(lambda c: c.audit.list(sandbox_id="sbx_1", limit=10, before_seq=9), "GET /v1/audit",
                   query={"sandbox_id": ["sbx_1"], "limit": ["10"], "before_seq": ["9"]})
        self.check(lambda c: c.audit.verify(), "GET /v1/audit/verify", {"ok": True, "count": 2})
        self.check(lambda c: c.audit.export(), "GET /v1/audit/export", [{"seq": 1}, {"seq": 2}])

    def test_benchmarks(self):
        self.check(lambda c: c.benchmarks.list(), "GET /v1/benchmarks", [{"name": "swe-bench-verified"}])
        self.check(lambda c: c.benchmarks.get("swe"), "GET /v1/benchmarks/swe", {"name": "swe"})
        self.check(lambda c: c.benchmarks.instances("swe", state="ready", limit=5), "GET /v1/benchmarks/swe/instances",
                   query={"state": ["ready"], "limit": ["5"]})
        self.check(lambda c: c.benchmarks.instance("swe", "a"), "GET /v1/benchmarks/swe/instances/a",
                   {"id": "a", "state": "ready"})
        self.check(lambda c: c.benchmarks.prepare("swe", ["a", "b"], seed=1), "POST /v1/benchmarks/swe/prepare",
                   body={"instances": ["a", "b"], "seed": 1})

    def test_rl(self):
        self.check(lambda c: c.rl.runs(project="p", limit=3), "GET /v1/rl/runs", query={"project": ["p"], "limit": ["3"]})
        self.check(lambda c: c.rl.run("run_1"), "GET /v1/rl/runs/run_1")
        rows = self.check(lambda c: c.rl.compare(["run_1", "run_2"]), "GET /v1/rl/compare",
                          query={"ids": ["run_1,run_2"]})
        self.assertEqual(len(rows), 2)
        self.check(lambda c: c.rl.alerts(), "GET /v1/rl/alerts")
        self.check(lambda c: c.rl.create_alert("infra_rate", 0.2, run_id="run_1"), "POST /v1/rl/alerts",
                   {"id": "rla_1"}, body={"kind": "infra_rate", "threshold": 0.2, "run_id": "run_1"})
        self.check(lambda c: c.rl.update_alert("rla_1", enabled=False), "PATCH /v1/rl/alerts/rla_1",
                   body={"enabled": False})
        self.check(lambda c: c.rl.delete_alert("rla_1"), "DELETE /v1/rl/alerts/rla_1")
        sync, _, _ = both(api)
        with self.assertRaises(ValueError):
            sync.rl.compare(["run_1"])

    def test_function_invocations(self):
        self.check(lambda c: c.functions.invocations("fn", limit=20), "GET /v1/functions/fn/invocations",
                   [{"id": "inv_1"}], query={"limit": ["20"]})

    def test_ai_gateway_url_and_usage(self):
        sync, aio, _ = both(api)
        self.assertEqual(sync.ai.gateway_url("openai"), "https://api.kosa8.com/v1/ai/gw/openai/")
        self.assertEqual(aio.ai.gateway_url("anthropic", "/v1/messages"),
                         "https://api.kosa8.com/v1/ai/gw/anthropic/v1/messages")
        with self.assertRaises(ValueError):
            sync.ai.gateway_url("openai", "../admin")
        self.check(lambda c: c.ai.usage("2026-11"), "GET /v1/ai/usage", query={"month": ["2026-11"]})

    def test_browsers_cdp(self):
        from kosa8.cloud_browser import CDP_PATH, cdp_path

        self.assertEqual(cdp_path("brw_1"), "/v1/browsers/brw_1/cdp")
        self.assertIn("{id}", CDP_PATH)
        self.check(lambda c: c.browsers.cdp_url("brw_1"), "GET /v1/browsers/brw_1",
                   "wss://x/v1/browsers/brw_1/cdp?token=t")

    def test_async_browser_context_closes(self):
        _, aio, stub = both(api)

        async def go():
            async with await aio.browser(ttl_s=60) as b:
                self.assertIsInstance(b, kosa8.AsyncCloudBrowser)
                await b.refresh()
            return b

        b = run(go())
        self.assertEqual(route(stub.seen[-1]), "DELETE /v1/browsers/brw_1")
        self.assertEqual(b.state, "closed")


class SandboxFeatures(unittest.TestCase):
    def box(self, cloud):
        from kosa8.aio import AsyncCloudSandbox
        from kosa8.cloud import CloudSandbox

        info = {"id": "sbx_1", "state": "running"}
        return AsyncCloudSandbox(cloud, info) if isinstance(cloud, AsyncCloud) else CloudSandbox(cloud, info)

    def test_background_exec_status_and_stop(self):
        sync, aio, stub = both(api)
        job = self.box(sync).exec_background("sleep 9", timeout_s=30)
        self.assertEqual(stub.seen[-1]["body"], {"cmd": ["sh", "-c", "sleep 9"], "background": True, "timeout_s": 30})
        self.assertEqual(job.stop()["state"], "stopping")
        self.assertEqual(route(stub.seen[-1]), "POST /v1/sandboxes/sbx_1/exec/bg_1/stop")
        self.assertEqual(job.status()["exit"], 0)
        self.assertEqual(route(stub.seen[-1]), "GET /v1/sandboxes/sbx_1/exec/bg_1")

        async def go():
            j = await self.box(aio).exec_background(["sleep", "9"])
            await j.stop()
            return await j.wait(timeout=5, interval=0)

        self.assertEqual(run(go())["output"], "hi\n")
        with self.assertRaises(ValueError):
            self.box(sync).exec_background("x", timeout_s=0)

    def test_item_deletes(self):
        sync, aio, stub = both(api)
        for c in (sync, aio):
            b = self.box(c)
            for call, want in (
                (lambda: b.previews.delete(3000), "DELETE /v1/sandboxes/sbx_1/previews/3000"),
                (lambda: b.schedules.delete("sch_1"), "DELETE /v1/sandboxes/sbx_1/schedules/sch_1"),
                (lambda: b.code_contexts.delete("default"), "DELETE /v1/sandboxes/sbx_1/code/contexts/default"),
            ):
                r = call()
                if asyncio.iscoroutine(r):
                    r = run(r)
                self.assertIsNone(r)
                self.assertEqual(route(stub.seen[-1]), want)

    def test_overflow_refusal_is_raised_as_sent(self):
        sync, aio, _ = both(api)
        box = sync.sandboxes.get("sbx_1")
        self.assertTrue(box.overflow)
        with self.assertRaises(Kosa8UnsupportedError) as e:
            box.fork(1)
        self.assertEqual(e.exception.code, "not_available")
        self.assertIn("overflow", str(e.exception))

    def test_small_size_create(self):
        self.assertIn("0.25x0.5", CLOUD_SIZES)
        sync, _, stub = both(lambda s: (202, {"sandbox": {"id": "sbx_9", "state": "running", "cpus": 0.25, "mem_mb": 512}}))
        sync.sandboxes.create(cpus=0.25, mem_mb=512, wait=False)
        self.assertEqual(stub.seen[-1]["body"]["cpus"], 0.25)
        self.assertEqual(stub.seen[-1]["body"]["mem_mb"], 512)
        sync.sandboxes.create(size="0.25x0.5", wait=False)
        self.assertEqual(stub.seen[-1]["body"]["size"], "0.25x0.5")


def flaky(*failures, ok=(200, {"exec": {"id": "bg_1", "state": "running"}})):
    """Answer each failure once, in order, then ``ok``."""
    left = list(failures)

    def answer(s):
        return left.pop(0) if left else ok
    return answer


BUSY = (503, {"error": "database_busy", "message": "busy", "failure_class": "infra", "retryable": True,
              "retry_after": 2, "retry_after_s": 2}, {"Retry-After": "2"})
BUILDING = (409, {"error": "template_building", "message": "building", "retry_after_s": 15, "eta_s": 60,
                  "failure_class": "infra", "retryable": True}, {"Retry-After": "15"})
LIMITED = (429, {"error": "rate_limited", "message": "slow down"}, {"Retry-After": "3"})


class Retries(unittest.TestCase):
    def setUp(self):
        self.waits = []
        p = mock.patch("kosa8.retry._sleep", self.waits.append)
        p.start()
        self.addCleanup(p.stop)

    def exec_bg(self, cloud):
        from kosa8.cloud import CloudSandbox

        return CloudSandbox(cloud, {"id": "sbx_1", "state": "running"}).exec_background("true")

    def test_database_busy_retries_an_exec_post(self):
        stub = Stub(flaky(BUSY))
        self.exec_bg(Kosa8Cloud(KEY, env={}, opener=stub))
        self.assertEqual(len(stub.seen), 2)
        self.assertGreaterEqual(self.waits[0], 2)

    def test_database_busy_body_only_retry_after(self):
        busy = (503, {"error": "database_busy", "message": "busy", "retry_after": 4})
        stub = Stub(flaky(busy))
        self.exec_bg(Kosa8Cloud(KEY, env={}, opener=stub))
        self.assertEqual(len(stub.seen), 2)
        self.assertGreaterEqual(self.waits[0], 4)

    def test_template_building_retries_a_create_waiting_its_eta(self):
        stub = Stub(flaky(BUILDING, ok=(201, {"sandbox": {"id": "sbx_2", "state": "running"}})))
        box = Kosa8Cloud(KEY, env={}, opener=stub).sandboxes.create(template="swe/a", wait=False)
        self.assertEqual(box.id, "sbx_2")
        self.assertEqual(len(stub.seen), 2)
        self.assertGreaterEqual(self.waits[0], 15)
        # The same Idempotency-Key on the retry.
        self.assertEqual(stub.seen[0]["headers"]["Idempotency-Key"], stub.seen[1]["headers"]["Idempotency-Key"])

    def test_429_retries_any_post_honouring_retry_after(self):
        stub = Stub(flaky(LIMITED, LIMITED))
        self.exec_bg(Kosa8Cloud(KEY, env={}, opener=stub))
        self.assertEqual(len(stub.seen), 3)
        self.assertTrue(all(w >= 3 for w in self.waits))

    def test_attempts_are_bounded(self):
        stub = Stub(lambda s: BUSY)
        with self.assertRaises(Kosa8ApiError) as e:
            self.exec_bg(Kosa8Cloud(KEY, env={}, opener=stub, retry=RetryPolicy(attempts=4)))
        self.assertEqual(e.exception.code, "database_busy")
        self.assertEqual(len(stub.seen), 4)

    def test_a_5xx_exec_is_not_resent(self):
        stub = Stub(flaky((502, {"error": "host_failed", "message": "boom"})))
        with self.assertRaises(Kosa8ApiError):
            self.exec_bg(Kosa8Cloud(KEY, env={}, opener=stub))
        self.assertEqual(len(stub.seen), 1)

    def test_a_5xx_read_is_retried(self):
        stub = Stub(flaky((502, {"error": "host_failed", "message": "boom"}), ok=(200, {"rates": {}})))
        Kosa8Cloud(KEY, env={}, opener=stub).pricing()
        self.assertEqual(len(stub.seen), 2)

    def test_retryable_body_is_honoured_on_reads(self):
        stub = Stub(flaky((409, {"error": "try_later", "message": "x", "retryable": True}), ok=(200, {})))
        Kosa8Cloud(KEY, env={}, opener=stub).limits()
        self.assertEqual(len(stub.seen), 2)
        stub = Stub(flaky((400, {"error": "bad", "message": "x", "retryable": False}), ok=(200, {})))
        with self.assertRaises(Kosa8ApiError):
            Kosa8Cloud(KEY, env={}, opener=stub).limits()
        self.assertEqual(len(stub.seen), 1)

    def test_async_retries_the_same(self):
        stub = Stub(flaky(BUSY, BUILDING, ok=(200, {"rates": {}})))

        async def go():
            with mock.patch("kosa8.retry._asleep", self._asleep):
                return await AsyncCloud(KEY, env={}, async_opener=stub.aopen).pricing()

        run(go())
        self.assertEqual(len(stub.seen), 3)
        self.assertGreaterEqual(self.waits[1], 15)

    async def _asleep(self, s):
        self.waits.append(s)

    def test_classify(self):
        from kosa8.cloud import _cloud_error

        for status, body in ((503, BUSY[1]), (409, BUILDING[1]),
                             (409, {"error": "template_building", "failure_class": "client", "retryable": False})):
            self.assertTrue(classify_failure(_cloud_error(status, body, "POST", "/v1/sandboxes"))[1], body)


if __name__ == "__main__":
    unittest.main()
