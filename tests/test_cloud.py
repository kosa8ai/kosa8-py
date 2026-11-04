import json
import unittest

from kosa8 import (
    Kosa8Cloud,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8NotFoundError,
    Kosa8PaymentRequiredError,
    Kosa8RateLimitError,
)

KEY = "k8c_live_testkey"


def sbx(**over):
    d = {"id": "sbx_1", "state": "running", "template": "python", "cpus": 1, "mem_mb": 512}
    d.update(over)
    return d


class Stub:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        s = {"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None}
        self.seen.append(s)
        status, payload = self.answer(s)
        return status, b"" if payload is None else json.dumps(payload).encode()


class CloudTest(unittest.TestCase):
    def test_key_and_address(self):
        c = Kosa8Cloud(env={"KOSA8_API_KEY": KEY})
        self.assertEqual(c.base_url, "https://api.kosa8.cloud")
        self.assertNotIn(KEY, repr(c))
        with self.assertRaisesRegex(Kosa8Error, "KOSA8_API_KEY"):
            Kosa8Cloud(env={})
        with self.assertRaisesRegex(Kosa8Error, "k8c_live_"):
            Kosa8Cloud("sk-other", env={})
        with self.assertRaisesRegex(Kosa8Error, "https"):
            Kosa8Cloud(KEY, base_url="http://cp.example.com", env={})
        self.assertEqual(Kosa8Cloud(KEY, base_url="http://localhost:8787/", env={}).base_url, "http://localhost:8787")

    def test_sandbox_lifecycle(self):
        def answer(s):
            u, m = s["url"], s["method"]
            if m == "POST" and u.endswith("/v1/sandboxes"):
                return 201, {"sandbox": sbx(), "host_ms": 6}
            if u.endswith("/exec"):
                return 200, {"exit": 0, "stdout": "42\n", "stderr": "", "woke_ms": 70}
            if u.endswith("/sleep"):
                return 200, {"sandbox": sbx(state="sleeping")}
            if u.endswith("/wake"):
                return 200, {"sandbox": sbx()}
            if m == "DELETE":
                return 200, {"ok": True, "id": "sbx_1"}
            return 404, {"error": "not_found", "message": "no route"}

        stub = Stub(answer)
        cloud = Kosa8Cloud(KEY, base_url="https://cp.test", env={}, opener=stub)
        box = cloud.sandboxes.create(template="python", sleep_after_s=600)
        self.assertEqual(stub.seen[0]["body"], {"template": "python", "sleep_after_s": 600})
        self.assertEqual(stub.seen[0]["headers"]["Authorization"], f"Bearer {KEY}")
        r = box.exec('python3 -c "print(6*7)"', env={"A": "1"})
        self.assertEqual(stub.seen[1]["body"], {"cmd": ["sh", "-c", 'python3 -c "print(6*7)"'], "env": ["A=1"]})
        self.assertEqual((r.exit_code, r.stdout, r.woke_ms), (0, "42\n", 70))
        self.assertEqual(box.sleep().state, "sleeping")
        self.assertEqual(box.wake().state, "running")
        box.delete()
        self.assertEqual(
            [(s["method"], s["url"].replace("https://cp.test", "")) for s in stub.seen],
            [("POST", "/v1/sandboxes"), ("POST", "/v1/sandboxes/sbx_1/exec"), ("POST", "/v1/sandboxes/sbx_1/sleep"),
             ("POST", "/v1/sandboxes/sbx_1/wake"), ("DELETE", "/v1/sandboxes/sbx_1")],
        )

    def test_functions(self):
        stub = Stub(lambda s: (200, {"invocation": {"id": "inv_1"}, "ok": True, "result": {"hello": "kosa8"}})
                    if s["url"].endswith("/invoke") else (200, {"count": 2, "results": []}))
        cloud = Kosa8Cloud(KEY, env={}, opener=stub)
        self.assertEqual(cloud.functions.invoke("hello", {"name": "kosa8"})["result"], {"hello": "kosa8"})
        self.assertEqual(stub.seen[0]["body"], {"event": {"name": "kosa8"}})
        cloud.functions.map("hello", [1, 2], concurrency=4)
        self.assertEqual(stub.seen[1]["body"], {"events": [1, 2], "concurrency": 4})

    def test_typed_errors_keep_the_message_and_code(self):
        answers = [
            (402, {"error": "payment_required", "message": "add a payment method to start running work"}),
            (429, {"error": "quota_exceeded", "message": "this organization already has 20 active sandboxes"}),
            (404, {"error": "not_found", "message": "no such sandbox"}),
        ]
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: answers.pop(0)))
        with self.assertRaises(Kosa8PaymentRequiredError) as e:
            cloud.sandboxes.create()
        self.assertEqual(e.exception.code, "payment_required")
        self.assertEqual(str(e.exception), "add a payment method to start running work")
        self.assertEqual(e.exception.status, 402)
        with self.assertRaises(Kosa8RateLimitError) as e:
            cloud.sandboxes.create()
        self.assertEqual(e.exception.code, "quota_exceeded")
        with self.assertRaises(Kosa8NotFoundError):
            cloud.sandboxes.get("sbx_x")

    def test_unreachable(self):
        def boom(*a):
            raise OSError("connection refused")

        cloud = Kosa8Cloud(KEY, env={}, opener=boom)
        with self.assertRaisesRegex(Kosa8ConnectionError, "could not reach kosa8 Cloud"):
            cloud.me()


if __name__ == "__main__":
    unittest.main()
