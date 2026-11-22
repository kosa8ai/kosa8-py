# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Burst Passes, capability tokens and desktop recordings, on both clients."""
import asyncio
import json
import unittest
import urllib.parse

import kosa8
from kosa8 import Kosa8Cloud, Kosa8Error

KEY = "k8c_live_testkey"
LOCAL = "http://127.0.0.1:9"

TOKEN = {"id": "tok_1", "name": "agent", "prefix": "k8c_tok_ab", "scopes": ["sandboxes:read"], "state": "active"}
REC = {"id": "rec_1", "sandbox_id": "sbx_1", "state": "ready", "bytes": 1024, "error": None}
LINK = {"url": "https://r2.example/rec_1.mp4?sig=x", "expires_at": 1, "bytes": 1024}


def answer(s):
    r = f"{s['method']} {s['path']}"
    if r == "GET /v1/billing/burst":
        return 200, {"available": True, "pass": {"id": "burst_1", "hours": 24}, "held": 1, "active": [],
                     "adds_now": {"running": 0, "sandbox_hours": 0}}
    if r == "POST /v1/billing/burst":
        return 200, {"ok": True, "activated": s["body"]["quantity"], "held": 0}
    if r == "POST /v1/tokens":
        return 201, {"token": {**TOKEN, "secret": "k8c_tok_secret"}}
    if r == "GET /v1/tokens":
        return 200, {"tokens": [TOKEN]}
    if r == "DELETE /v1/tokens/tok_1":
        return 200, {"token": {**TOKEN, "state": "revoked"}}
    if r == "GET /v1/recordings":
        return 200, {"recordings": [REC], "configured": True, "retention_days": 30}
    if r == "POST /v1/recordings/rec_1/link":
        return 200, {"link": LINK}
    if r == "DELETE /v1/recordings/rec_1":
        return 204, None
    if r == "GET /v1/sandboxes/sbx_1/recordings":
        return 200, {"recordings": [REC]}
    if r == "POST /v1/sandboxes":
        return 201, {"sandbox": {"id": "sbx_1", "state": "running"}}
    return 404, {"error": {"code": "not_found", "message": r}}


class Stub:
    def __init__(self):
        self.seen = []

    def _record(self, method, url, body):
        u = urllib.parse.urlsplit(url)
        s = {"method": method, "path": u.path, "query": urllib.parse.parse_qs(u.query),
             "body": json.loads(body) if body else None}
        self.seen.append(s)
        status, payload = answer(s)
        return status, (b"" if payload is None else json.dumps(payload).encode())

    def __call__(self, method, url, headers, body, timeout):
        return self._record(method, url, body)

    async def aopen(self, method, url, headers, body, timeout):
        return self._record(method, url, body)


def make():
    stub = Stub()
    return Kosa8Cloud(KEY, env={}, opener=stub), stub


class Burst(unittest.TestCase):
    def test_get_and_activate(self):
        cloud, stub = make()
        self.assertTrue(cloud.burst.get()["available"])
        self.assertEqual(cloud.burst.activate(2)["activated"], 2)
        self.assertEqual(stub.seen[-1]["body"], {"quantity": 2})
        self.assertEqual(cloud.burst.activate()["activated"], 1)

    def test_quantity_is_checked_before_any_request(self):
        cloud, stub = make()
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                cloud.burst.activate(bad)
        self.assertEqual(stub.seen, [])


class Tokens(unittest.TestCase):
    def test_create_list_revoke(self):
        cloud, stub = make()
        t = cloud.tokens.create(["sandboxes:read"], sandbox_ids=["sbx_1"], labels={"team": "ml"}, ttl_s=600, name="agent")
        self.assertEqual(t["secret"], "k8c_tok_secret")
        self.assertEqual(stub.seen[-1]["body"], {"scopes": ["sandboxes:read"], "sandbox_ids": ["sbx_1"],
                                                  "labels": {"team": "ml"}, "ttl_s": 600, "name": "agent"})
        self.assertEqual(cloud.tokens.list()[0]["id"], "tok_1")
        self.assertEqual(stub.seen[-1]["query"], {})
        cloud.tokens.list(all=True)
        self.assertEqual(stub.seen[-1]["query"], {"all": ["1"]})
        self.assertEqual(cloud.tokens.revoke("tok_1")["state"], "revoked")

    def test_checks_before_any_request(self):
        cloud, stub = make()
        with self.assertRaises(ValueError):
            cloud.tokens.create([])
        with self.assertRaises(ValueError):
            cloud.tokens.create(["sandboxes:read"], sandbox_ids=[])
        with self.assertRaises(ValueError):
            cloud.tokens.create(["sandboxes:read"], sandbox_ids=[f"sbx_{i}" for i in range(101)])
        for bad in (59, 86_401, 1.5):
            with self.assertRaises(ValueError):
                cloud.tokens.create(["sandboxes:read"], ttl_s=bad)
        self.assertEqual(stub.seen, [])

    def test_a_token_is_a_key(self):
        c = Kosa8Cloud("k8c_tok_secret", env={}, opener=Stub())
        self.assertEqual(c.tokens.list()[0]["id"], "tok_1")
        with self.assertRaises(Kosa8Error):
            Kosa8Cloud("k8c_nope", env={})


class Recordings(unittest.TestCase):
    def test_list_link_delete(self):
        cloud, stub = make()
        r = cloud.recordings.list(sandbox_id="sbx_1", limit=5)
        self.assertEqual(r, {"recordings": [REC], "configured": True, "retention_days": 30})
        self.assertEqual(stub.seen[-1]["query"], {"sandbox_id": ["sbx_1"], "limit": ["5"]})
        self.assertEqual(cloud.recordings.link("rec_1", ttl_s=120), LINK)
        self.assertEqual(stub.seen[-1]["body"], {"ttl_s": 120})
        cloud.recordings.link("rec_1")
        self.assertEqual(stub.seen[-1]["body"], {})
        self.assertIsNone(cloud.recordings.delete("rec_1"))
        self.assertEqual(stub.seen[-1]["method"], "DELETE")

    def test_checks(self):
        cloud, stub = make()
        with self.assertRaises(ValueError):
            cloud.recordings.list(limit=0)
        with self.assertRaises(ValueError):
            cloud.recordings.link("rec_1", ttl_s=10)
        self.assertEqual(stub.seen, [])

    def test_a_sandbox_recordings_and_record_on_create(self):
        cloud, stub = make()
        box = cloud.sandboxes.create(image="kosa8-desktop", persistent=True, record=True)
        self.assertIs(stub.seen[0]["body"]["record"], True)
        self.assertEqual(box.recordings(), [REC])
        self.assertEqual(stub.seen[-1]["path"], "/v1/sandboxes/sbx_1/recordings")


class AsyncTwins(unittest.TestCase):
    def test_async_twin(self):
        stub = Stub()

        async def main():
            async with kosa8.AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=stub.aopen) as cloud:
                burst = await cloud.burst.get()
                act = await cloud.burst.activate(3)
                tok = await cloud.tokens.create(["sandboxes:write"], ttl_s=60)
                toks = await cloud.tokens.list(all=True)
                rev = await cloud.tokens.revoke("tok_1")
                recs = await cloud.recordings.list()
                link = await cloud.recordings.link("rec_1")
                await cloud.recordings.delete("rec_1")
                box = await cloud.sandboxes.create(record=True)
                mine = await box.recordings()
                return burst, act, tok, toks, rev, recs, link, mine

        burst, act, tok, toks, rev, recs, link, mine = asyncio.run(main())
        self.assertTrue(burst["available"])
        self.assertEqual(act["activated"], 3)
        self.assertEqual(tok["secret"], "k8c_tok_secret")
        self.assertEqual(len(toks), 1)
        self.assertEqual(rev["state"], "revoked")
        self.assertTrue(recs["configured"])
        self.assertEqual(link["url"], LINK["url"])
        self.assertEqual(mine, [REC])
        routes = [f"{s['method']} {s['path']}" for s in stub.seen]
        self.assertIn("DELETE /v1/recordings/rec_1", routes)
        self.assertIn("GET /v1/sandboxes/sbx_1/recordings", routes)


if __name__ == "__main__":
    unittest.main()
