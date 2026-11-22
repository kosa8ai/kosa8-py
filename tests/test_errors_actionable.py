"""kosa8 Cloud errors say what happened and what to do next: the server's
sentence, its code and status, when to retry, and the request id."""

import asyncio
import json
import socket
import unittest
import urllib.error

from kosa8 import (
    Kosa8Cloud,
    Kosa8ConflictError,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8PaymentRequiredError,
    Kosa8AuthError,
    Kosa8TimeoutError,
    Kosa8UnsupportedError,
    Kosa8ApiError,
    RetryPolicy,
)
from kosa8.aio import AsyncCloud

KEY = "k8c_live_testkey"
NO_RETRY = RetryPolicy(attempts=1)


def opener(status, body, headers=None):
    def call(method, url, h, payload, timeout):
        return status, json.dumps(body).encode(), headers or {}
    return call


def raised(cloud, fn):
    try:
        fn(cloud)
    except Kosa8Error as e:
        return e
    raise AssertionError("no error raised")


class CloudErrorTest(unittest.TestCase):
    def test_no_capacity_shows_message_code_retry_and_request_id(self):
        body = {"error": "no_capacity", "message": "no capacity: fork fewer or retry in 10 s",
                "retryable": True, "retry_after_s": 10, "failure_class": "infra"}
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY,
                           opener=opener(503, body, {"Retry-After": "10", "cf-ray": "a5e77ddee9ef8120-BOS"}))
        e = raised(cloud, lambda c: c.sandboxes.create())
        self.assertIsInstance(e, Kosa8ApiError)
        self.assertEqual(e.message, "no capacity: fork fewer or retry in 10 s")
        self.assertEqual(e.code, "no_capacity")
        self.assertEqual(e.status, 503)
        self.assertEqual(e.retry_after, 10.0)
        self.assertEqual(e.retry_after_s, 10.0)
        self.assertTrue(e.retryable)
        self.assertEqual(e.request_id, "a5e77ddee9ef8120-BOS")
        self.assertEqual(
            str(e), "no capacity: fork fewer or retry in 10 s [HTTP 503 no_capacity; retry in 10 s; request a5e77ddee9ef8120-BOS]")

    def test_x_request_id_wins_over_cf_ray(self):
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(
            404, {"error": "not_found", "message": "no such sandbox"}, {"x-request-id": "req_1", "CF-Ray": "r-2"}))
        e = raised(cloud, lambda c: c.sandboxes.get("sbx_x"))
        self.assertEqual(e.request_id, "req_1")
        self.assertFalse(e.retryable)
        self.assertEqual(str(e), "no such sandbox [HTTP 404 not_found; request req_1]")

    def test_template_building_reads_retry_after_s_from_the_body(self):
        body = {"error": "template_building", "message": "the environment of swe/x is being built; ready in about 120 s",
                "eta_s": 120, "retry_after_s": 30, "failure_class": "quota", "retryable": True}
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(409, body))
        e = raised(cloud, lambda c: c.sandboxes.create())
        self.assertIsInstance(e, Kosa8ConflictError)
        self.assertEqual(e.retry_after, 30.0)
        self.assertTrue(e.retryable)
        self.assertEqual(e.body["eta_s"], 120)
        self.assertIn("[HTTP 409 template_building; retry in 30 s]", str(e))

    def test_retry_after_http_date(self):
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(
            429, {"error": "rate_limited", "message": "slow down"}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        e = raised(cloud, lambda c: c.sandboxes.list())
        self.assertEqual(e.retry_after, 0.0)  # a date in the past: no wait
        self.assertIn("retryable", str(e))

    def test_payment_required_points_at_the_upgrade_url(self):
        body = {"error": "payment_required", "message": "add a payment method to start running work",
                "upgrade_url": "https://app.kosa8.com/app#billing", "agent_message": "Tell the person to add one."}
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(402, body))
        e = raised(cloud, lambda c: c.sandboxes.create())
        self.assertIsInstance(e, Kosa8PaymentRequiredError)
        self.assertFalse(e.retryable)
        self.assertEqual(e.upgrade_url, "https://app.kosa8.com/app#billing")
        self.assertEqual(str(e), "add a payment method to start running work "
                                 "[HTTP 402 payment_required; upgrade: https://app.kosa8.com/app#billing]")

    def test_unknown_key_says_what_to_do(self):
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(
            401, {"error": "unauthorized", "message": "API key is unknown, revoked or expired"}))
        e = raised(cloud, lambda c: c.sandboxes.list())
        self.assertIsInstance(e, Kosa8AuthError)
        self.assertEqual(e.daemon_message, "API key is unknown, revoked or expired")
        self.assertIn("KOSA8_API_KEY", str(e))
        self.assertIn("https://app.kosa8.com/app#keys", str(e))
        self.assertTrue(str(e).endswith("[HTTP 401 unauthorized]"))

    def test_not_available_is_not_retryable(self):
        cloud = Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=opener(
            501, {"error": "not_available", "message": "background commands are not available on this sandbox's host yet"}))
        e = raised(cloud, lambda c: c.sandboxes.list())
        self.assertIsInstance(e, Kosa8UnsupportedError)
        self.assertFalse(e.retryable)
        self.assertEqual(e.code, "not_available")
        self.assertTrue(str(e).endswith("[HTTP 501 not_available]"))

    def test_plain_text_answer_still_shows_the_status(self):
        def call(method, url, h, payload, timeout):
            return 502, b"Bad gateway", {}
        e = raised(Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=call), lambda c: c.sandboxes.list())
        self.assertEqual(str(e), "Bad gateway [HTTP 502; retryable]")


class ClientSideErrorTest(unittest.TestCase):
    def test_missing_key(self):
        with self.assertRaises(Kosa8Error) as e:
            Kosa8Cloud(env={})
        self.assertIn("KOSA8_API_KEY", str(e.exception))
        self.assertIn("api_key=", str(e.exception))
        self.assertIn("https://app.kosa8.com/app#keys", str(e.exception))

    def test_wrong_key_prefix(self):
        with self.assertRaises(Kosa8Error) as e:
            Kosa8Cloud("sk-not-kosa8", env={})
        self.assertIn("k8c_live_", str(e.exception))
        self.assertIn("https://app.kosa8.com/app#keys", str(e.exception))

    def test_timeout_says_how_to_wait_longer(self):
        def call(*a):
            raise socket.timeout("timed out")
        e = raised(Kosa8Cloud(KEY, env={}, retry=NO_RETRY, timeout=5, opener=call), lambda c: c.sandboxes.list())
        self.assertIsInstance(e, Kosa8TimeoutError)
        self.assertIn("within 5 s", str(e))
        self.assertIn("timeout=", str(e))
        self.assertTrue(e.retryable)
        self.assertIsNone(e.request_id)

    def test_unreachable_says_what_to_check(self):
        def call(*a):
            raise urllib.error.URLError("nodename nor servname provided")
        e = raised(Kosa8Cloud(KEY, env={}, retry=NO_RETRY, opener=call), lambda c: c.sandboxes.list())
        self.assertIsInstance(e, Kosa8ConnectionError)
        self.assertIn("KOSA8_CLOUD_URL", str(e))
        self.assertIn("try again", str(e))

    def test_async_client_has_the_same_fields(self):
        async def aopen(method, url, h, payload, timeout):
            return 503, json.dumps({"error": "no_capacity", "message": "kosa8 Cloud is full",
                                    "retry_after_s": 10}).encode(), {"Retry-After": "10", "cf-ray": "abc-SJC"}

        async def go():
            async with AsyncCloud(KEY, env={}, retry=NO_RETRY, async_opener=aopen) as cloud:
                try:
                    await cloud.sandboxes.list()
                except Kosa8Error as e:
                    return e
        e = asyncio.run(go())
        self.assertEqual((e.code, e.status, e.retry_after, e.request_id), ("no_capacity", 503, 10.0, "abc-SJC"))
        self.assertEqual(str(e), "kosa8 Cloud is full [HTTP 503 no_capacity; retry in 10 s; request abc-SJC]")


if __name__ == "__main__":
    unittest.main()
