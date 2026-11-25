"""A signed-in CLI (`kosa8 login`) is enough for the SDK: with no
KOSA8_API_KEY, the key and address come from ~/.kosa8/cloud.json."""

import json
import os
import tempfile
import unittest

from kosa8 import (
    Kosa8Cloud,
    Kosa8Error,
    Kosa8PaymentRequiredError,
    Kosa8PlanLimitError,
    Kosa8RateLimitError,
)
from kosa8.errors import error_from_response


def home_with(creds):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".kosa8"))
    if creds is not None:
        with open(os.path.join(d, ".kosa8", "cloud.json"), "w") as f:
            f.write(creds if isinstance(creds, str) else json.dumps(creds))
    return d


class LoginFallbackTest(unittest.TestCase):
    def test_uses_cloud_json_when_env_key_unset(self):
        home = home_with({"key": "k8c_live_fromlogin", "api_url": "http://localhost:8787", "org_id": "org_1"})
        c = Kosa8Cloud(env={"HOME": home})
        self.assertEqual(c._key, "k8c_live_fromlogin")
        self.assertEqual(c.base_url, "http://localhost:8787")

    def test_env_key_wins_and_saved_url_is_not_used_for_it(self):
        home = home_with({"key": "k8c_live_fromlogin", "api_url": "http://localhost:8787"})
        c = Kosa8Cloud(env={"HOME": home, "KOSA8_API_KEY": "k8c_live_env"})
        self.assertEqual(c._key, "k8c_live_env")
        self.assertEqual(c.base_url, "https://api.kosa8.com")

    def test_env_url_wins_over_saved_url(self):
        home = home_with({"key": "k8c_live_fromlogin", "api_url": "http://localhost:8787"})
        c = Kosa8Cloud(env={"HOME": home, "KOSA8_CLOUD_URL": "http://localhost:9999"})
        self.assertEqual(c.base_url, "http://localhost:9999")

    def test_no_file_or_bad_file_says_kosa8_login(self):
        for creds in (None, "not json", {"org_id": "x"}):
            with self.assertRaisesRegex(Kosa8Error, "kosa8 login"):
                Kosa8Cloud(env={"HOME": home_with(creds)})

    def test_explicit_empty_key_does_not_read_the_file(self):
        home = home_with({"key": "k8c_live_fromlogin"})
        with self.assertRaises(Kosa8Error):
            Kosa8Cloud("", env={"HOME": home})


class StarterLimitClassTest(unittest.TestCase):
    def test_429_starter_limit_is_payment_required(self):
        e = error_from_response(429, {"error": "Starter runs 2 sandboxes at once"}, code="starter_limit")
        self.assertIsInstance(e, Kosa8PaymentRequiredError)
        self.assertIsInstance(e, Kosa8PlanLimitError)
        self.assertIsInstance(e, Kosa8RateLimitError)  # code written before keeps working
        self.assertFalse(e.retryable)

    def test_through_the_client(self):
        def call(method, url, h, payload, timeout):
            return 429, json.dumps({"error": "starter_limit", "message": "Starter runs 2 sandboxes at once"}).encode(), {}
        from kosa8 import RetryPolicy
        c = Kosa8Cloud("k8c_live_x", env={}, opener=call, retry=RetryPolicy(attempts=1))
        with self.assertRaises(Kosa8PaymentRequiredError):
            c.sandboxes.create()

    def test_402_starter_limit_stays_plain_payment_required(self):
        e = error_from_response(402, {"error": "x"}, code="starter_limit")
        self.assertIs(type(e), Kosa8PaymentRequiredError)

    def test_plain_rate_limit_unchanged(self):
        e = error_from_response(429, {"error": "slow down"}, code="rate_limited")
        self.assertIs(type(e), Kosa8RateLimitError)


if __name__ == "__main__":
    unittest.main()
