"""Serverless runs (POST /v1/run, GET /v1/runs) and direct-exec tokens."""

import asyncio
import json
import unittest
import urllib.parse

import kosa8
from kosa8 import Kosa8Cloud, Kosa8PaymentRequiredError
from kosa8.aio import AsyncCloud, AsyncCloudSandbox
from kosa8.cloud import CloudSandbox

KEY = "k8c_live_testkey"


class Fake:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        self.seen.append({"method": method, "path": u.path, "query": u.query,
                          "body": json.loads(body) if body else None, "timeout": timeout})
        st, payload = self.answer(self.seen[-1])
        return st, json.dumps(payload).encode(), {}

    async def aopen(self, *a):
        return self(*a)


RUN = {"id": "sr_1", "status": "ok", "exit": 0, "stdout": "42\n", "stderr": "", "files": []}


class Runs(unittest.TestCase):
    def test_run_code(self):
        fake = Fake(lambda s: (200, {"run": RUN}))
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        r = cloud.run("print(6*7)", language="python", timeout_s=600, outputs=["/workspace/out.txt"],
                      env={"A": "1"}, files={"data.txt": "x"})
        self.assertEqual(r["stdout"], "42\n")
        s = fake.seen[0]
        self.assertEqual((s["method"], s["path"]), ("POST", "/v1/run"))
        self.assertEqual(s["body"], {"code": "print(6*7)", "language": "python", "timeout_s": 600,
                                     "outputs": ["/workspace/out.txt"], "env": {"A": "1"}, "files": {"data.txt": "x"}})
        self.assertGreaterEqual(s["timeout"], 660)  # waits past the run's own timeout
        cloud.run(command=["ls", "-la"])
        self.assertEqual(fake.seen[1]["body"], {"command": ["ls", "-la"]})
        self.assertGreaterEqual(fake.seen[1]["timeout"], 120)

    def test_run_checks_arguments(self):
        cloud = Kosa8Cloud(KEY, env={}, opener=Fake(lambda s: (200, {"run": RUN})))
        with self.assertRaises(ValueError):
            cloud.run()
        with self.assertRaises(ValueError):
            cloud.run("x", timeout_s=3601)

    def test_run_refusal_is_typed_and_not_resent(self):
        fake = Fake(lambda s: (402, {"error": "plan_required", "message": "pick a plan",
                                     "upgrade_url": "https://app.kosa8.com/app#billing"}))
        with self.assertRaises(Kosa8PaymentRequiredError) as e:
            Kosa8Cloud(KEY, env={}, opener=fake).run("print(1)")
        self.assertEqual(e.exception.upgrade_url, "https://app.kosa8.com/app#billing")
        self.assertEqual(len(fake.seen), 1)

    def test_runs_list_get_share(self):
        fake = Fake(lambda s: (200, {"runs": [RUN]} if s["path"] == "/v1/runs" else {"run": RUN}))
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        self.assertEqual(cloud.runs.list(limit=5), [RUN])
        self.assertEqual(fake.seen[-1]["query"], "limit=5")
        self.assertEqual(cloud.runs.get("sr_1")["id"], "sr_1")
        cloud.runs.share("sr_1", True, code=False)
        self.assertEqual((fake.seen[-1]["path"], fake.seen[-1]["body"]), ("/v1/runs/sr_1/share", {"public": True, "code": False}))

    def test_async_run_and_direct(self):
        direct = {"exec_url": "https://h/exec", "token": "t", "expires_at": 1}
        fake = Fake(lambda s: (200, {"direct": direct} if s["path"].endswith("/direct") else {"run": RUN}))

        async def go():
            async with AsyncCloud(KEY, env={}, async_opener=fake.aopen) as cloud:
                r = await cloud.run("print(1)")
                d = await AsyncCloudSandbox(cloud, {"id": "sbx_1", "state": "running"}).direct()
                return r, d

        r, d = asyncio.run(go())
        self.assertEqual((r["id"], d), ("sr_1", direct))

    def test_direct(self):
        fake = Fake(lambda s: (200, {"direct": {"exec_url": "u", "token": "t", "expires_at": 1}}))
        d = CloudSandbox(Kosa8Cloud(KEY, env={}, opener=fake), {"id": "sbx_1", "state": "running"}).direct()
        self.assertEqual(d["token"], "t")
        self.assertEqual((fake.seen[0]["method"], fake.seen[0]["path"]), ("POST", "/v1/sandboxes/sbx_1/direct"))

    def test_exported(self):
        self.assertTrue(hasattr(kosa8, "CloudRuns") and hasattr(kosa8, "AsyncCloudRuns"))


if __name__ == "__main__":
    unittest.main()
