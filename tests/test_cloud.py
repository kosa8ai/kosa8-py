import json
import socket
import unittest
import urllib.parse

from kosa8 import (
    DEFAULT_CLOUD_URL,
    Kosa8ApiError,
    Kosa8Cloud,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8NotFoundError,
    Kosa8OrgFrozenError,
    Kosa8PaymentRequiredError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
    Kosa8UnsupportedError,
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
        raw = headers.get("Content-Type") == "application/octet-stream"
        s = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body if raw else (json.loads(body) if body else None),
        }
        self.seen.append(s)
        out = self.answer(s)
        status, payload = out[0], out[1]
        data = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        return (status, data) + tuple(out[2:])


def path_of(s):
    u = urllib.parse.urlsplit(s["url"])
    return u.path, dict(urllib.parse.parse_qsl(u.query))


def cloud_box(cloud):
    from kosa8 import CloudSandbox

    return CloudSandbox(cloud, sbx())


class CloudTest(unittest.TestCase):
    def test_key_and_address(self):
        c = Kosa8Cloud(env={"KOSA8_API_KEY": KEY})
        self.assertEqual(c.base_url, "https://api.kosa8.com")
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

    def test_default_address_is_api_kosa8_com(self):
        self.assertEqual(DEFAULT_CLOUD_URL, "https://api.kosa8.com")
        self.assertEqual(Kosa8Cloud(KEY, env={}).base_url, "https://api.kosa8.com")
        self.assertEqual(
            Kosa8Cloud(KEY, env={"KOSA8_CLOUD_URL": "https://cp.example.com/"}).base_url, "https://cp.example.com"
        )
        seen = []
        Kosa8Cloud(KEY, env={}, opener=lambda m, u, h, b, t: seen.append(u) or (200, b"{}")).me()
        self.assertEqual(seen, ["https://api.kosa8.com/v1/me"])

    def test_create_lifetime_and_validation(self):
        stub = Stub(lambda s: (201, {"sandbox": sbx(delete_after=123)}))
        cloud = Kosa8Cloud(KEY, env={}, opener=stub)
        cloud.sandboxes.create(ttl_s=3600)
        self.assertEqual(stub.seen[-1]["body"], {"ttl_s": 3600})
        cloud.sandboxes.create(template="acme/tool@1.2.3", delete_after=1900000000000, accept_price=0.5, region="eu-hel1")
        self.assertEqual(
            stub.seen[-1]["body"],
            {"template": "acme/tool@1.2.3", "delete_after": 1900000000000, "accept_price": 0.5, "region": "eu-hel1"},
        )
        for bad in (dict(ttl_s=59), dict(ttl_s=7776001), dict(ttl_s=True), dict(ttl_s=60, delete_after=1),
                    dict(sleep_after_s=-1), dict(sleep_after_s=604801)):
            with self.assertRaises(ValueError, msg=bad):
                cloud.sandboxes.create(**bad)
        self.assertEqual(len(stub.seen), 2)  # nothing sent for a bad argument

    def test_update(self):
        stub = Stub(lambda s: (200, {"sandbox": sbx(delete_after=99, sleep_after_s=0)}))
        cloud = Kosa8Cloud(KEY, env={}, opener=stub)
        box = cloud_box(cloud)
        self.assertEqual(box.update(ttl_s=600, sleep_after_s=0).info["delete_after"], 99)
        self.assertEqual((stub.seen[-1]["method"], path_of(stub.seen[-1])[0]), ("PATCH", "/v1/sandboxes/sbx_1"))
        self.assertEqual(stub.seen[-1]["body"], {"ttl_s": 600, "sleep_after_s": 0})
        box.update(clear_lifetime=True)
        self.assertEqual(stub.seen[-1]["body"], {"ttl_s": None})
        with self.assertRaises(ValueError):
            box.update()
        with self.assertRaises(ValueError):
            box.update(clear_lifetime=True, ttl_s=60)

    def test_list_pages_and_falls_back(self):
        pages = {
            None: {"sandboxes": [sbx(id="sbx_1"), sbx(id="sbx_2")], "next_cursor": "c2"},
            "c2": {"sandboxes": [sbx(id="sbx_3")], "next_cursor": None},
        }
        stub = Stub(lambda s: (200, pages[path_of(s)[1].get("cursor")]))
        cloud = Kosa8Cloud(KEY, env={}, opener=stub)
        page = cloud.sandboxes.list_page(limit=2, all=True)
        self.assertEqual([b.id for b in page.sandboxes], ["sbx_1", "sbx_2"])
        self.assertEqual(page.next_cursor, "c2")
        self.assertEqual(path_of(stub.seen[-1])[1], {"all": "1", "limit": "2"})
        self.assertEqual([b.id for b in cloud.sandboxes.iter(page_size=2)], ["sbx_1", "sbx_2", "sbx_3"])
        self.assertEqual(path_of(stub.seen[-1])[1], {"limit": "2", "cursor": "c2"})
        self.assertEqual([b.id for b in cloud.sandboxes.list(cursor="c2")], ["sbx_3"])

        # A server without pagination: no next_cursor, everything at once.
        old = Stub(lambda s: (200, {"sandboxes": [sbx(id="sbx_9")]}))
        cloud = Kosa8Cloud(KEY, env={}, opener=old)
        self.assertIsNone(cloud.sandboxes.list_page(limit=5).next_cursor)
        self.assertEqual([b.id for b in cloud.sandboxes.iter()], ["sbx_9"])
        self.assertEqual(stub.seen[0]["url"].split("?")[0], "https://api.kosa8.com/v1/sandboxes")

        # A server that repeats its cursor does not loop forever.
        loop = Stub(lambda s: (200, {"sandboxes": [sbx()], "next_cursor": "same"}))
        cloud = Kosa8Cloud(KEY, env={}, opener=loop)
        self.assertEqual(len(list(cloud.sandboxes.iter())), 2)
        with self.assertRaises(ValueError):
            cloud.sandboxes.list(limit=0)

    def test_files(self):
        def answer(s):
            p, q = path_of(s)
            if p.endswith("/files") and s["method"] == "GET":
                return 200, b"\x00bin\xff"
            if p.endswith("/files") and s["method"] == "PUT":
                return 200, {"file": {"path": q["path"], "size": len(s["body"]), "type": "file"}}
            if p.endswith("/files") and s["method"] == "DELETE":
                return 200, {"removed": q["path"]}
            if p.endswith("/files/list"):
                return 200, {"path": q["path"], "files": [{"name": "a.py", "path": "/workspace/a.py", "type": "file"}]}
            if p.endswith("/files/mkdir"):
                return 200, {"file": {"path": s["body"]["path"], "type": "dir"}}
            return 404, {"error": "not_found", "message": "?"}

        stub = Stub(answer)
        box = cloud_box(Kosa8Cloud(KEY, env={}, opener=stub))
        self.assertEqual(box.files.read("/workspace/a b.bin"), b"\x00bin\xff")
        self.assertEqual(path_of(stub.seen[-1]), ("/v1/sandboxes/sbx_1/files", {"path": "/workspace/a b.bin"}))
        self.assertEqual(stub.seen[-1]["headers"]["Accept"], "application/octet-stream")
        info = box.files.write("/workspace/app.py", "print('hi')\n")
        self.assertEqual(info["size"], 12)
        self.assertEqual(stub.seen[-1]["body"], b"print('hi')\n")
        self.assertEqual(stub.seen[-1]["headers"]["Content-Type"], "application/octet-stream")
        box.files.write("/workspace/x.bin", b"\x01\x02")
        self.assertEqual(stub.seen[-1]["body"], b"\x01\x02")
        self.assertEqual(box.files.list()[0]["name"], "a.py")
        self.assertEqual(path_of(stub.seen[-1]), ("/v1/sandboxes/sbx_1/files/list", {"path": "/workspace"}))
        self.assertEqual(box.files.mkdir("/workspace/d")["type"], "dir")
        box.files.delete("/workspace/d", recursive=True)
        self.assertEqual(path_of(stub.seen[-1])[1], {"path": "/workspace/d", "recursive": "1"})
        self.assertEqual(stub.seen[-1]["method"], "DELETE")
        with self.assertRaises(ValueError):
            box.files.read("")

    def test_checkpoint_rewind_fork_ports_templates(self):
        def answer(s):
            p, _ = path_of(s)
            if p.endswith("/checkpoints") and s["method"] == "POST":
                return 201, {"checkpoint": {"id": "ckp_1", "label": "before"}, "evicted": [], "ms": 40}
            if p.endswith("/checkpoints"):
                return 200, {"checkpoints": [{"id": "ckp_1"}], "max": 10}
            if p.endswith("/rewind"):
                return 200, {"sandbox": sbx(state="running", cpus=2), "checkpoint": {"id": "ckp_1"},
                             "backup": {"id": "ckp_2", "auto": True}, "ms": 80}
            if p.endswith("/fork"):
                return 201, {"parent": "sbx_1", "sandboxes": [sbx(id="sbx_f1", source="fork", parent_id="sbx_1"),
                                                             sbx(id="sbx_f2", source="fork", parent_id="sbx_1")]}
            if p.endswith("/ports"):
                return 200, {"ports": [{"port": 8000, "address": "0.0.0.0", "loopback": False}]}
            if p == "/v1/templates":
                return 200, {"templates": [{"key": "base"}, {"key": "python"}]}
            return 404, {"error": "not_found", "message": "?"}

        stub = Stub(answer)
        cloud = Kosa8Cloud(KEY, env={}, opener=stub)
        box = cloud_box(cloud)
        self.assertEqual(box.checkpoint("before")["id"], "ckp_1")
        self.assertEqual(stub.seen[-1]["body"], {"label": "before"})
        box.checkpoint()
        self.assertEqual(stub.seen[-1]["body"], {})
        self.assertEqual([c["id"] for c in box.checkpoints()], ["ckp_1"])
        r = box.rewind("ckp_1")
        self.assertEqual((r.checkpoint["id"], r.backup["id"], r.ms), ("ckp_1", "ckp_2", 80))
        self.assertEqual(stub.seen[-1]["body"], {"checkpoint_id": "ckp_1"})
        self.assertEqual(box.info["cpus"], 2)
        kids = box.fork(2)
        self.assertEqual([k.id for k in kids], ["sbx_f1", "sbx_f2"])
        self.assertEqual(kids[0].info["parent_id"], "sbx_1")
        self.assertEqual(stub.seen[-1]["body"], {"n": 2})
        self.assertEqual(box.ports()[0]["port"], 8000)
        self.assertEqual([t["key"] for t in cloud.templates()], ["base", "python"])
        for bad in (0, 101, True):
            with self.assertRaises(ValueError):
                box.fork(bad)
        with self.assertRaises(ValueError):
            box.checkpoint("x" * 201)
        with self.assertRaises(ValueError):
            box.rewind("")

    def test_dataplane_not_supported(self):
        for code in ("not_available", "not_supported"):
            cloud = Kosa8Cloud(KEY, env={}, opener=Stub(
                lambda s, c=code: (501, {"error": c, "message": "checkpoints is not available on this host yet"})))
            with self.assertRaises(Kosa8UnsupportedError) as e:
                cloud_box(cloud).checkpoint()
            self.assertEqual(e.exception.code, code)

    def test_payment_required_exposes_upgrade_url_and_agent_message(self):
        body = {"error": "spending_cap_reached", "message": "the spending cap is reached",
                "upgrade_url": "https://app.kosa8.com/#billing", "agent_message": "Tell your person to raise the cap."}
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (402, body)))
        with self.assertRaises(Kosa8PaymentRequiredError) as e:
            cloud.sandboxes.create()
        self.assertEqual(e.exception.code, "spending_cap_reached")
        self.assertEqual(e.exception.upgrade_url, "https://app.kosa8.com/#billing")
        self.assertEqual(e.exception.agent_message, "Tell your person to raise the cap.")
        bare = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (402, {"error": "payment_required", "message": "pay"})))
        with self.assertRaises(Kosa8PaymentRequiredError) as e:
            bare.sandboxes.create()
        self.assertIsNone(e.exception.upgrade_url)
        self.assertIsNone(e.exception.agent_message)

    def test_org_frozen_and_rate_limit(self):
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (423, {"error": "org_frozen", "message": "frozen"})))
        with self.assertRaises(Kosa8OrgFrozenError) as e:
            cloud.sandboxes.create()
        self.assertEqual((e.exception.status, e.exception.code), (423, "org_frozen"))
        self.assertIsInstance(e.exception, Kosa8ApiError)
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(
            lambda s: (429, {"error": "rate_limited", "message": "slow down"}, {"retry-after": "7"})))
        with self.assertRaises(Kosa8RateLimitError) as e:
            cloud.sandboxes.list()
        self.assertEqual(e.exception.retry_after, 7.0)
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (429, {"error": "quota_exceeded", "message": "q"})))
        with self.assertRaises(Kosa8RateLimitError) as e:
            cloud.sandboxes.create()
        self.assertIsNone(e.exception.retry_after)

    def test_exec_timeout(self):
        cases = [
            (502, {"error": "host_failed", "message": "exec failed on the host: host unreachable: timed out"}),
            (504, {"error": "exec_timeout", "message": "the command ran past 30 s"}),
            (408, {"error": "exec_timeout", "message": "the command ran past 30 s"}),
        ]
        for status, body in cases:
            cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s, a=(status, body): a))
            with self.assertRaises(Kosa8ExecTimeoutError) as e:
                cloud_box(cloud).exec("sleep 40")
            self.assertIsInstance(e.exception, Kosa8TimeoutError)
            self.assertIsInstance(e.exception, Kosa8ApiError)
            self.assertEqual(e.exception.status, status)
            self.assertIn("30 s", str(e.exception))
        # A 502 that is not a timeout, or not on exec, stays a plain API error.
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (502, {"error": "host_failed", "message": "boom"})))
        with self.assertRaises(Kosa8ApiError) as e:
            cloud_box(cloud).exec("true")
        self.assertNotIsInstance(e.exception, Kosa8ExecTimeoutError)
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (502, {"error": "host_failed", "message": "timed out"})))
        with self.assertRaises(Kosa8ApiError) as e:
            cloud_box(cloud).sleep()
        self.assertNotIsInstance(e.exception, Kosa8ExecTimeoutError)

        def slow(*a):
            raise socket.timeout("timed out")

        with self.assertRaises(Kosa8ExecTimeoutError):
            cloud_box(Kosa8Cloud(KEY, env={}, opener=slow)).exec("sleep 200")
        with self.assertRaises(Kosa8TimeoutError) as e:
            Kosa8Cloud(KEY, env={}, opener=slow).me()
        self.assertNotIsInstance(e.exception, Kosa8ExecTimeoutError)

    def test_exec_null_exit(self):
        cloud = Kosa8Cloud(KEY, env={}, opener=Stub(lambda s: (200, {"exit": None, "stdout": "", "stderr": "killed"})))
        self.assertIsNone(cloud_box(cloud).exec("true").exit_code)

    def test_unreachable(self):
        def boom(*a):
            raise OSError("connection refused")

        cloud = Kosa8Cloud(KEY, env={}, opener=boom)
        with self.assertRaisesRegex(Kosa8ConnectionError, "could not reach kosa8 Cloud"):
            cloud.me()


if __name__ == "__main__":
    unittest.main()
