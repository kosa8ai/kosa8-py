import json
import os
import tempfile
import threading
import unittest

from kosa8 import (
    FileCheckpointStore,
    Kosa8ApprovalRequiredError,
    Kosa8BadRequestError,
    Kosa8Client,
    Kosa8ConflictError,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8NotFoundError,
    Kosa8PolicyError,
    Kosa8StreamError,
    MemoryCheckpointStore,
    resolve_endpoint,
)

from mock_daemon import MockDaemon, send_frames, send_json


def sb(name, **extra):
    return {"id": name, "name": name, "state": "running", **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = MockDaemon().start()
        self.c = Kosa8Client(socket_path=self.d.socket_path, env={}, checkpoint_store=MemoryCheckpointStore())

    def tearDown(self):
        self.d.stop()


class StatusTest(Base):
    def test_health_over_unix_socket_tags_actor(self):
        self.d.on("GET", "/kosa8/v1/health", lambda r, h: send_json(h, 200, {"status": "ok", "version": "0.12.0"}))
        self.assertEqual(self.c.version(), "0.12.0")
        hdr = {k.lower(): v for k, v in self.d.requests[0].headers.items()}
        self.assertEqual(hdr["x-kosa8-actor"], "tool")
        self.assertNotIn("x-kosa8-approval", hdr)

    def test_unreachable_daemon_is_connection_error(self):
        c = Kosa8Client(socket_path=os.path.join(tempfile.gettempdir(), "nope-k8.sock"), env={})
        with self.assertRaises(Kosa8ConnectionError) as cm:
            c.health()
        self.assertIn("daemon running", str(cm.exception))


class SandboxTest(Base):
    def test_create_maps_options_and_sends_approval(self):
        self.d.on(
            "POST",
            "/kosa8/v1/sandboxes",
            lambda r, h: send_json(h, 201, {"sandbox": sb(r.json()["name"]), "boot_ms": 412}),
        )
        out = self.c.create_sandbox(
            name="demo", cpus=2, mem_mb=2048, disk_gb=8,
            egress_allow=["api.anthropic.com", "*.npmjs.org"], approval="req-1",
        )
        self.assertEqual(out["sandbox"]["name"], "demo")
        req = self.d.find("POST", "/kosa8/v1/sandboxes")[0]
        self.assertEqual(
            req.json(),
            {"name": "demo", "cpus": 2, "mem_mb": 2048, "disk_gb": 8, "egress": "allow:api.anthropic.com,*.npmjs.org"},
        )
        self.assertEqual({k.lower(): v for k, v in req.headers.items()}["x-kosa8-approval"], "req-1")

    def test_egress_forms(self):
        self.d.on("POST", "/kosa8/v1/sandboxes", lambda r, h: send_json(h, 201, {"sandbox": sb("x")}))
        self.c.create_sandbox(egress="deny")
        self.c.create_sandbox(egress={"allow": ["10.0.0.0/8"]})
        bodies = [r.json() for r in self.d.find("POST", "/kosa8/v1/sandboxes")]
        self.assertEqual(bodies[0]["egress"], "deny")
        self.assertEqual(bodies[1]["egress"], "allow:10.0.0.0/8")
        with self.assertRaises(Kosa8Error):
            self.c.create_sandbox(egress={"allow": []})
        with self.assertRaises(Kosa8Error):
            self.c.create_sandbox(egress="any", egress_allow=["a.com"])

    def test_get_sandbox_by_id_or_name(self):
        self.d.on("GET", "/kosa8/v1/sandboxes", lambda r, h: send_json(h, 200, [{"id": "s1", "name": "alpha"}]))
        self.assertEqual(self.c.get_sandbox("alpha")["id"], "s1")
        self.assertIsNone(self.c.get_sandbox("nope"))

    def test_path_segments_are_encoded(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/down", lambda r, h: send_json(h, 200, {"ok": True}))
        self.c.stop_sandbox("a b/c")
        self.assertEqual(self.d.requests[0].url, "/kosa8/v1/sandboxes/a%20b%2Fc/down")


class ErrorTest(Base):
    def test_typed_errors_carry_daemon_words(self):
        cases = [
            (400, Kosa8BadRequestError),
            (404, Kosa8NotFoundError),
            (409, Kosa8ConflictError),
            (403, Kosa8PolicyError),
        ]
        for code, cls in cases:
            self.d.on("GET", "/kosa8/v1/sandboxes",
                      lambda r, h, code=code: send_json(h, code, {"error": "nope", "remediation": "do x"}))
            with self.assertRaises(cls) as cm:
                self.c.list_sandboxes()
            self.assertEqual(str(cm.exception), "nope (do x)")
            self.assertEqual(cm.exception.status, code)
            self.assertEqual(cm.exception.remediation, "do x")

    def test_approval_refusal(self):
        refusal = {"why": "nothing presented", "request_command": "kosa8 approve request ...", "route": "x"}
        self.d.on("DELETE", "/kosa8/v1/sandboxes/{id}",
                  lambda r, h: send_json(h, 403, {"error": "approval required", "approval": refusal}))
        with self.assertRaises(Kosa8ApprovalRequiredError) as cm:
            self.c.delete_sandbox("s1")
        self.assertEqual(cm.exception.approval["why"], "nothing presented")


class StreamTest(Base):
    def test_run_collects_frames_and_calls_back(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/run", lambda r, h: send_frames(h, [
            {"stream": "status", "data": "pulling"},
            {"stream": "stdout", "data": "he"},
            {"stream": "stdout", "data": "llo\n"},
            {"stream": "stderr", "data": "warn\n"},
            {"done": True, "ok": True, "exit": 3},
        ]))
        seen = []
        res = self.c.run("s1", "alpine", ["sh", "-c", "x"], env={"A": "1"}, on_stdout=seen.append)
        self.assertEqual(res.exit_code, 3)
        self.assertFalse(res.ok)
        self.assertEqual(res.stdout, "hello\n")
        self.assertEqual(res.stderr, "warn\n")
        self.assertEqual(seen, [b"he", b"llo\n"])
        self.assertEqual(self.d.requests[0].json(), {"image": "alpine", "args": ["sh", "-c", "x"], "env": ["A=1"]})

    def test_error_frame_keeps_output(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/exec", lambda r, h: send_frames(h, [
            {"stream": "stdout", "data": "partial"},
            {"done": True, "error": "container gone"},
        ]))
        with self.assertRaises(Kosa8StreamError) as cm:
            self.c.exec("s1", "c1", ["ls"])
        self.assertEqual(cm.exception.stdout, "partial")

    def test_stream_without_final_frame(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/exec",
                  lambda r, h: send_frames(h, [{"stream": "stdout", "data": "x"}]))
        with self.assertRaises(Kosa8StreamError):
            self.c.exec("s1", "c1", ["ls"])

    def test_write_stdin_streams_chunked_iterable(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/containers/{cid}/stdin",
                  lambda r, h: send_json(h, 200, {"ok": True}))
        self.c.write_stdin("s1", "c1", iter([b"ab", "cd"]))
        self.assertEqual(self.d.requests[0].body, b"abcd")


class ForkExploreTest(Base):
    def test_fork_body(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/fork",
                  lambda r, h: send_json(h, 200, {"forks": [sb("p1"), sb("p2")], "count": 2}))
        out = self.c.fork("s1", 2, prefix="p")
        self.assertEqual(out["count"], 2)
        self.assertEqual(self.d.requests[0].json(), {"n": 2, "prefix": "p"})

    def test_explore_keeps_fastest_success_and_deletes_rest(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/pull", lambda r, h: send_json(h, 200, {"ok": True}))
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/fork",
                  lambda r, h: send_json(h, 200, {"forks": [sb("b1"), sb("b2")], "count": 2}))

        def run(r, h):
            variant = r.json()["args"][2]
            send_frames(h, [{"stream": "stdout", "data": variant}, {"done": True, "exit": 0 if variant == "ok" else 1}])

        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/run", run)
        self.d.on("DELETE", "/kosa8/v1/sandboxes/{id}", lambda r, h: send_json(h, 200, {"ok": True}))
        res = self.c.explore("s1", ["fail", "ok"], prefix="b")
        self.assertEqual(res.winner, "b2")
        self.assertEqual(res.removed, 1)
        self.assertEqual([r.url for r in self.d.find("DELETE", "/kosa8/v1/sandboxes/")], ["/kosa8/v1/sandboxes/b1"])

    def test_explore_needs_two_variants(self):
        with self.assertRaises(Kosa8Error):
            self.c.explore("s1", ["only"])


class CheckpointTest(Base):
    def _routes(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/{id}/snapshot",
                  lambda r, h: send_json(h, 201, {"id": r.json()["id"]}))
        self.d.on("DELETE", "/kosa8/v1/sandboxes/{id}", lambda r, h: send_json(h, 200, {"ok": True}))
        self.d.on("DELETE", "/kosa8/v1/snapshots/{id}", lambda r, h: send_json(h, 200, {"ok": True}))
        self.d.on("POST", "/kosa8/v1/snapshots/{id}/restore",
                  lambda r, h: send_json(h, 200, {"sandbox": sb(r.json()["name"]), "restore_ms": 494}))

    def test_checkpoint_then_rewind(self):
        self._routes()
        a = self.c.checkpoint("s1", label="before")
        b = self.c.checkpoint("s1", label="after")
        self.assertEqual(a["checkpoint"]["id"], "s1-ckpt-1")
        self.assertEqual(b["checkpoint"]["id"], "s1-ckpt-2")
        out = self.c.rewind("s1", to="BEFORE")
        self.assertEqual(out["checkpoint"]["id"], "s1-ckpt-1")
        self.assertEqual(out["dropped"], ["s1-ckpt-2"])
        self.assertEqual(out["restore_ms"], 494)
        self.assertEqual([c["id"] for c in self.c.list_checkpoints("s1")], ["s1-ckpt-1"])
        restore = self.d.find("POST", "/kosa8/v1/snapshots/s1-ckpt-1/restore")[0]
        self.assertEqual(restore.json(), {"name": "s1"})

    def test_eviction_past_ten(self):
        self._routes()
        for _ in range(11):
            out = self.c.checkpoint("s1")
        self.assertEqual(out["evicted"], ["s1-ckpt-1"])
        self.assertEqual(len(self.c.list_checkpoints("s1")), 10)

    def test_rewind_without_checkpoints(self):
        with self.assertRaises(Kosa8Error):
            self.c.rewind("s1")

    def test_bad_sandbox_name_rejected(self):
        with self.assertRaises(Kosa8Error):
            self.c.checkpoint("../etc")


class FileStoreTest(unittest.TestCase):
    def test_file_store_is_cli_compatible_and_concurrent(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "checkpoints.json")
        store = FileCheckpointStore(p)
        # A record as the Go CLI writes it (nanosecond RFC 3339).
        with open(p, "w") as f:
            json.dump({"s1": [{"id": "s1-ckpt-1", "sandbox": "s1", "label": "go",
                               "created": "2026-11-03T10:00:00.123456789Z"}]}, f)
        from kosa8 import CheckpointBook
        book = CheckpointBook(store)

        def add(i):
            book.record(f"s2-ckpt-{i}", "s2", str(i))

        ts = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(book.list("s2")), 20)
        self.assertEqual(book.latest("s1")["label"], "go")
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(p + ".lock"))


class EndpointTest(unittest.TestCase):
    def test_default_socket(self):
        e = resolve_endpoint(env={})
        self.assertEqual(e.kind, "unix")
        self.assertTrue(e.socket_path.endswith(".kosa8/run/kosa8.sock"))

    def test_host_rules(self):
        e = resolve_endpoint(env={"KOSA8_HOST": "https://w.example.com/", "KOSA8_TOKEN": "t"})
        self.assertEqual((e.kind, e.scheme, e.host, e.token), ("tcp", "https", "w.example.com", "t"))
        self.assertEqual(resolve_endpoint(env={"KOSA8_HOST": "tcp://127.0.0.1:7440"}).port, 7440)
        self.assertEqual(resolve_endpoint(env={"KOSA8_HOST": "unix:///tmp/x.sock"}).socket_path, "/tmp/x.sock")
        with self.assertRaises(Kosa8Error):
            resolve_endpoint(env={"KOSA8_HOST": "http://w.example.com"})
        self.assertEqual(resolve_endpoint(env={"KOSA8_HOST": "http://w.example.com", "KOSA8_INSECURE": "1"}).kind, "tcp")
        with self.assertRaises(Kosa8Error):
            resolve_endpoint(env={"KOSA8_HOST": "ftp://x"})
        # The env token is never sent to a host named explicitly.
        self.assertIsNone(resolve_endpoint(host="https://other", env={"KOSA8_HOST": "https://w", "KOSA8_TOKEN": "t"}).token)


if __name__ == "__main__":
    unittest.main()
