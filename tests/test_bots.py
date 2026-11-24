"""Autopilot: cloud.bots, cloud.approvals and the human_required error."""

import asyncio
import json
import unittest
import urllib.parse

import kosa8
from kosa8 import Kosa8Cloud
from kosa8.aio import AsyncCloud
from kosa8.retry import RetryPolicy

KEY = "k8c_live_testkey"
BOT = {"id": "bot_1", "name": "Spend guard", "state": "draft", "template": "spend-guard@1"}
RUN = {"id": "brun_1", "bot_id": "bot_1", "dry_run": True, "state": "succeeded", "steps": []}
APPR = {"id": "appr_1", "run_id": "brun_1", "state": "pending", "tier": "T3", "action": "sandbox.delete"}


class Fake:
    def __init__(self, answer=None):
        self.answer = answer or default_answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        self.seen.append({"method": method, "path": u.path, "raw": url.split("?")[0], "query": u.query,
                          "body": json.loads(body) if body else None})
        st, payload = self.answer(self.seen[-1])
        return st, json.dumps(payload).encode(), {}

    async def aopen(self, *a):
        return self(*a)

    def calls(self):
        return [(s["method"], s["path"] + ("?" + s["query"] if s["query"] else ""), s["body"]) for s in self.seen]


def default_answer(s):
    p, m = s["path"], s["method"]
    if p == "/v1/bots" and m == "GET":
        return 200, {"bots": [BOT], "paused_all": False, "paused_all_at": None, "pending_approvals": 2}
    if p == "/v1/bot-templates":
        return 200, {"templates": [{"id": "spend-guard", "rank": 1}]}
    if p.startswith("/v1/bot-templates/"):
        return 200, {"template": {"id": "spend-guard"}}
    if p in ("/v1/bots/plan", "/v1/bots/compile"):
        return 200, {"plan": {"summary": "s"}, "spec": {"triggers": []}}
    if p in ("/v1/bots/pause-all", "/v1/bots/resume-all"):
        return 200, {"paused_all": p.endswith("pause-all"), "at": 1}
    if p.endswith("/runs") and m == "GET":
        return 200, {"runs": [RUN]}
    if p.endswith("/runs") or p.startswith("/v1/bot-runs/"):
        return 201 if m == "POST" and p.startswith("/v1/bots/") else 200, {"run": RUN}
    if p.startswith("/v1/bots") and m == "DELETE":
        return 200, {"deleted": True}
    if p.startswith("/v1/bots"):
        return 201 if p == "/v1/bots" else 200, {"bot": BOT}
    if p == "/v1/approvals":
        return 200, {"approvals": [APPR]}
    if p.endswith("/decide"):
        return 200, {"approval": {**APPR, "state": "approved"}, "run": RUN}
    if p.startswith("/v1/approvals/"):
        return 200, {"approval": APPR}
    return 404, {"error": "not_found", "message": "no"}


SPEC = {"triggers": [{"event": "sandbox.idle", "for_s": 1200}], "actions": [{"action": "sandbox.sleep"}]}


class Bots(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.cloud = Kosa8Cloud(KEY, env={}, opener=self.fake, retry=RetryPolicy(attempts=1))

    def test_every_bot_call(self):
        b = self.cloud.bots
        self.assertEqual(b.list()["pending_approvals"], 2)
        self.assertEqual(b.templates()[0]["id"], "spend-guard")
        self.assertEqual(b.template("spend-guard")["id"], "spend-guard")
        self.assertIn("plan", b.plan(template="spend-guard", replay=True))
        b.plan(spec=SPEC, name="n")
        self.assertIn("spec", b.compile("sleep idle dev sandboxes after 20 minutes"))
        self.assertEqual(b.create(template="spend-guard", name="g", budget_usd=5)["id"], "bot_1")
        b.create(spec=SPEC, sponsor="a@b.co", scope={"labels": {"env": "dev"}}, notify={"mode": "digest"})
        self.assertEqual(b.get("bot_1")["state"], "draft")
        b.update("bot_1", name="x", spec=SPEC)
        self.assertTrue(b.delete("bot_1")["deleted"])
        b.enable("bot_1")
        b.disable("bot_1")
        self.assertTrue(b.run("bot_1")["dry_run"])
        b.run("bot_1", dry_run=False)
        self.assertEqual(b.runs("bot_1", limit=5)[0]["id"], "brun_1")
        b.get_run("brun_1")
        b.cancel_run("brun_1")
        b.undo_step("brun_1", "s1")
        self.assertTrue(b.pause_all()["paused_all"])
        self.assertFalse(b.resume_all()["paused_all"])
        self.assertEqual(self.fake.calls(), [
            ("GET", "/v1/bots", None),
            ("GET", "/v1/bot-templates", None),
            ("GET", "/v1/bot-templates/spend-guard", None),
            ("POST", "/v1/bots/plan", {"template": "spend-guard", "replay": True}),
            ("POST", "/v1/bots/plan", {"spec": SPEC, "name": "n"}),
            ("POST", "/v1/bots/compile", {"text": "sleep idle dev sandboxes after 20 minutes"}),
            ("POST", "/v1/bots", {"template": "spend-guard", "name": "g", "budget_usd": 5}),
            ("POST", "/v1/bots", {"spec": SPEC, "sponsor": "a@b.co", "scope": {"labels": {"env": "dev"}},
                                  "notify": {"mode": "digest"}}),
            ("GET", "/v1/bots/bot_1", None),
            ("PATCH", "/v1/bots/bot_1", {"name": "x", "spec": SPEC}),
            ("DELETE", "/v1/bots/bot_1", None),
            ("POST", "/v1/bots/bot_1/enable", {}),
            ("POST", "/v1/bots/bot_1/disable", {}),
            ("POST", "/v1/bots/bot_1/runs", {"dry_run": True}),
            ("POST", "/v1/bots/bot_1/runs", {"dry_run": False}),
            ("GET", "/v1/bots/bot_1/runs?limit=5", None),
            ("GET", "/v1/bot-runs/brun_1", None),
            ("POST", "/v1/bot-runs/brun_1/cancel", {}),
            ("POST", "/v1/bot-runs/brun_1/steps/s1/undo", {}),
            ("POST", "/v1/bots/pause-all", {}),
            ("POST", "/v1/bots/resume-all", {}),
        ])

    def test_approvals(self):
        a = self.cloud.approvals
        self.assertIs(self.cloud.bots.approvals, a)
        self.assertEqual(a.list()[0]["id"], "appr_1")
        a.list("all", limit=10)
        a.get("appr_1")
        self.assertEqual(a.approve("appr_1")["approval"]["state"], "approved")
        a.approve("appr_1", reason="ok", edited_args={"spending_cap_usd": 50})
        a.reject("appr_1", reason="no")
        self.assertEqual(self.fake.calls(), [
            ("GET", "/v1/approvals?state=pending", None),
            ("GET", "/v1/approvals?state=all&limit=10", None),
            ("GET", "/v1/approvals/appr_1", None),
            ("POST", "/v1/approvals/appr_1/decide", {"decision": "approve"}),
            ("POST", "/v1/approvals/appr_1/decide", {"decision": "approve", "reason": "ok",
                                                      "edited_args": {"spending_cap_usd": 50}}),
            ("POST", "/v1/approvals/appr_1/decide", {"decision": "reject", "reason": "no"}),
        ])

    def test_checks_before_any_request(self):
        b = self.cloud.bots
        with self.assertRaisesRegex(ValueError, "exactly one"):
            b.create()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            b.create(template="spend-guard", spec=SPEC)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            b.plan()
        with self.assertRaisesRegex(ValueError, "500"):
            b.compile("x" * 501)
        b.compile("x" * 500)  # the limit itself is fine
        self.fake.seen.clear()
        with self.assertRaisesRegex(ValueError, "required"):
            b.compile("")
        with self.assertRaisesRegex(ValueError, "nothing to update"):
            b.update("bot_1")
        with self.assertRaisesRegex(ValueError, "limit"):
            b.runs("bot_1", limit=0)
        with self.assertRaisesRegex(ValueError, "state"):
            self.cloud.approvals.list("open")
        with self.assertRaisesRegex(ValueError, "required"):
            b.get("")
        self.assertEqual(self.fake.seen, [])

    def test_ids_are_escaped(self):
        self.cloud.bots.get("bot 1/../x")
        self.cloud.bots.undo_step("brun_1", "a/b")
        self.cloud.approvals.approve("appr?1")
        self.assertEqual([s["raw"].split("kosa8.com", 1)[-1] for s in self.fake.seen], [
            "/v1/bots/bot%201%2F..%2Fx",
            "/v1/bot-runs/brun_1/steps/a%2Fb/undo",
            "/v1/approvals/appr%3F1/decide",
        ])

    def test_human_required(self):
        refused = Fake(lambda s: (403, {"error": "human_required",
                                        "message": "a person must approve this: open app.kosa8.com/app#autopilot or Slack"}))
        cloud = Kosa8Cloud(KEY, env={}, opener=refused, retry=RetryPolicy(attempts=1))
        for call in (lambda: cloud.approvals.approve("appr_1"), lambda: cloud.approvals.reject("appr_1"),
                     lambda: cloud.bots.enable("bot_1"), lambda: cloud.bots.resume_all()):
            with self.assertRaises(kosa8.HumanApprovalRequired) as cm:
                call()
            e = cm.exception
            self.assertIsInstance(e, kosa8.Kosa8HumanApprovalRequiredError)
            self.assertIsInstance(e, kosa8.Kosa8PolicyError)
            self.assertIsInstance(e, kosa8.Kosa8ApiError)
            self.assertEqual((e.status, e.code), (403, "human_required"))
            self.assertIn("person", str(e))
        self.assertEqual(len(refused.seen), 4)  # no retry, no bypass
        other = Fake(lambda s: (403, {"error": "forbidden", "message": "scope"}))
        c2 = Kosa8Cloud(KEY, env={}, opener=other, retry=RetryPolicy(attempts=1))
        with self.assertRaises(kosa8.Kosa8PolicyError) as cm:
            c2.bots.enable("bot_1")
        self.assertNotIsInstance(cm.exception, kosa8.HumanApprovalRequired)

    def test_async(self):
        fake = Fake()

        async def go():
            async with AsyncCloud(KEY, env={}, async_opener=fake.aopen) as cloud:
                self.assertEqual((await cloud.bots.list())["bots"][0]["id"], "bot_1")
                self.assertEqual((await cloud.bots.create(template="spend-guard"))["id"], "bot_1")
                self.assertTrue((await cloud.bots.run("bot_1"))["dry_run"])
                self.assertEqual((await cloud.approvals.list())[0]["id"], "appr_1")
                self.assertIs(cloud.bots.approvals, cloud.approvals)
                await cloud.bots.approvals.reject("appr_1")
                with self.assertRaises(ValueError):
                    await cloud.bots.create(template="a", spec=SPEC)

        asyncio.run(go())
        self.assertEqual(fake.calls(), [
            ("GET", "/v1/bots", None),
            ("POST", "/v1/bots", {"template": "spend-guard"}),
            ("POST", "/v1/bots/bot_1/runs", {"dry_run": True}),
            ("GET", "/v1/approvals?state=pending", None),
            ("POST", "/v1/approvals/appr_1/decide", {"decision": "reject"}),
        ])

    def test_async_human_required(self):
        fake = Fake(lambda s: (403, {"error": "human_required", "message": "a person must approve this"}))

        async def go():
            async with AsyncCloud(KEY, env={}, async_opener=fake.aopen, retry=RetryPolicy(attempts=1)) as cloud:
                with self.assertRaises(kosa8.HumanApprovalRequired):
                    await cloud.bots.enable("bot_1")
                with self.assertRaises(kosa8.HumanApprovalRequired):
                    await cloud.approvals.approve("appr_1")

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
