# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""The SDK's own Idempotency-Key is renewed after a 5xx (each retry of a
server-side failure is a new attempt), kept after no answer, a 429 or a 409,
and a key the caller passed is never changed."""

import asyncio
import json
import unittest
from unittest import mock

from kosa8 import AsyncCloud, Kosa8Cloud
from kosa8.cloud import renews_idempotency_key
from kosa8.errors import Kosa8ApiError, Kosa8ConnectionError

KEY = "k8c_live_testkey"
HOST_FAILED = (502, {"error": "host_failed", "message": "boot failed", "failure_class": "infra", "retryable": True})
RESTARTING = (503, {"error": "host_restarting", "message": "restarting", "retryable": True}, {"Retry-After": "1"})
LIMITED = (429, {"error": "rate_limited", "message": "slow down"}, {"Retry-After": "1"})
IN_PROGRESS = (409, {"error": "idempotency_in_progress", "message": "still running", "retry_after_s": 1})
CREATED = (201, {"sandbox": {"id": "sbx_1", "state": "running"}})


def dropped():
    return OSError("connection reset by peer")


class Script:
    """A fake transport that gives its answers in turn (an exception is raised:
    no answer) and keeps the Idempotency-Key of every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.keys = []

    def __call__(self, method, url, headers, body, timeout):
        self.keys.append(headers.get("Idempotency-Key"))
        out = self.answers.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out[0], json.dumps(out[1]).encode(), out[2] if len(out) > 2 else {}

    async def aopen(self, *a):
        return self(*a)


class IdempotencyKeys(unittest.TestCase):
    def setUp(self):
        self.waits = []
        p = mock.patch("kosa8.retry._sleep", self.waits.append)
        p.start()
        self.addCleanup(p.stop)

        async def asleep(s):
            self.waits.append(s)

        p = mock.patch("kosa8.retry._asleep", asleep)
        p.start()
        self.addCleanup(p.stop)

    def create(self, script, **kw):
        return Kosa8Cloud(KEY, env={}, opener=script).sandboxes.create(wait=False, **kw)

    def test_a_502_retry_gets_a_new_key(self):
        s = Script(HOST_FAILED, CREATED)
        self.assertEqual(self.create(s).id, "sbx_1")
        self.assertEqual(len(s.keys), 2)
        self.assertTrue(all(s.keys))
        self.assertNotEqual(s.keys[0], s.keys[1])

    def test_a_503_retry_gets_a_new_key_after_its_retry_after(self):
        s = Script(RESTARTING, CREATED)
        self.create(s)
        self.assertNotEqual(s.keys[0], s.keys[1])
        self.assertGreaterEqual(self.waits[0], 1)

    def test_no_answer_keeps_the_key(self):
        s = Script(dropped(), CREATED)
        self.create(s)
        self.assertEqual(len(s.keys), 2)
        self.assertEqual(s.keys[0], s.keys[1])

    def test_429_and_409_in_progress_keep_the_key(self):
        s = Script(LIMITED, IN_PROGRESS, CREATED)
        self.create(s)
        self.assertEqual(len(s.keys), 3)
        self.assertEqual(len(set(s.keys)), 1)

    def test_the_callers_key_is_never_changed(self):
        s = Script(HOST_FAILED, RESTARTING, CREATED)
        self.create(s, idempotency_key="caller-key")
        self.assertEqual(s.keys, ["caller-key"] * 3)

    def test_job_submit_and_batch_renew_too(self):
        s = Script(HOST_FAILED, (202, {"job_id": "job_1", "job": {"status": "queued"}}))
        Kosa8Cloud(KEY, env={}, opener=s)._request("POST", "/v1/jobs", {})
        self.assertNotEqual(s.keys[0], s.keys[1])
        s = Script(HOST_FAILED, (200, {"sandboxes": [], "failed": [], "requested": 0}))
        Kosa8Cloud(KEY, env={}, opener=s)._request("POST", "/v1/sandboxes/batch", {})
        self.assertNotEqual(s.keys[0], s.keys[1])

    def test_async_client_follows_the_same_rule(self):
        async def go(script, **kw):
            return await AsyncCloud(KEY, env={}, async_opener=script.aopen).sandboxes.create(wait=False, **kw)

        s = Script(HOST_FAILED, CREATED)
        asyncio.run(go(s))
        self.assertNotEqual(s.keys[0], s.keys[1])
        s = Script(dropped(), CREATED)
        asyncio.run(go(s))
        self.assertEqual(s.keys[0], s.keys[1])
        s = Script(HOST_FAILED, CREATED)
        asyncio.run(go(s, idempotency_key="mine"))
        self.assertEqual(s.keys, ["mine", "mine"])

    def test_renews_idempotency_key(self):
        self.assertTrue(renews_idempotency_key(Kosa8ApiError("x", status=502, code="host_failed")))
        self.assertTrue(renews_idempotency_key(Kosa8ApiError("x", status=503, code="no_capacity")))
        self.assertFalse(renews_idempotency_key(Kosa8ApiError("x", status=429, code="rate_limited")))
        self.assertFalse(renews_idempotency_key(Kosa8ApiError("x", status=409, code="idempotency_in_progress")))
        self.assertFalse(renews_idempotency_key(Kosa8ConnectionError("no answer")))


if __name__ == "__main__":
    unittest.main()
