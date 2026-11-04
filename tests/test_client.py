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
        self.c = Kosa8Client(**self.d.client_args, env={}, checkpoint_store=MemoryCheckpointStore())

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
        self.assertRegex(str(cm.exception), "daemon running|wsl setup")


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
        if os.name != "nt":  # POSIX modes
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(p + ".lock"))


class EndpointTest(unittest.TestCase):
    def test_default_socket(self):
        e = resolve_endpoint(env={}, worker_file=False)
        self.assertEqual(e.kind, "unix")
        self.assertTrue(e.socket_path.endswith(os.path.join(".kosa8", "run", "kosa8.sock")))

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


class SavedWorkerTest(unittest.TestCase):
    """The connection kosa8 wsl setup / kosa8 worker join saves, as the CLI reads it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "worker.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def save(self, w):
        with open(self.file, "w") as f:
            f.write(w if isinstance(w, str) else json.dumps(w))

    def test_used_when_kosa8_host_is_unset(self):
        self.save({"host": "http://127.0.0.1:7428", "token": "saved", "via": "wsl", "distro": "Ubuntu"})
        e = resolve_endpoint(env={}, worker_file=self.file)
        self.assertEqual((e.kind, e.scheme, e.host, e.port, e.token), ("tcp", "http", "127.0.0.1", 7428, "saved"))
        c = Kosa8Client(env={}, worker_file=self.file, checkpoint_store=MemoryCheckpointStore())
        self.assertEqual(c.transport.target, "http://127.0.0.1:7428")

    def test_kosa8_host_wins_and_gets_no_saved_token(self):
        self.save({"host": "http://127.0.0.1:7428", "token": "saved"})
        e = resolve_endpoint(env={"KOSA8_HOST": "https://w.example.com"}, worker_file=self.file)
        self.assertEqual((e.host, e.token), ("w.example.com", None))

    def test_skipped_for_socket_bad_file_blank_host(self):
        self.save({"host": "http://127.0.0.1:7428"})
        self.assertEqual(resolve_endpoint(env={}, socket_path="/s.sock", worker_file=self.file).socket_path, "/s.sock")
        self.save("{not json")
        self.assertEqual(resolve_endpoint(env={}, worker_file=self.file).kind, "unix")
        self.save({"host": "  ", "token": "t"})
        self.assertEqual(resolve_endpoint(env={}, worker_file=self.file).kind, "unix")
        self.assertEqual(resolve_endpoint(env={}, worker_file=os.path.join(self.dir, "missing.json")).kind, "unix")

    def test_plain_http_to_lan_only_when_paired_insecure(self):
        self.save({"host": "10.1.2.3:7428", "token": "t"})
        with self.assertRaisesRegex(Kosa8Error, "plain http"):
            resolve_endpoint(env={}, worker_file=self.file)
        self.save({"host": "10.1.2.3:7428", "token": "t", "insecure": True})
        self.assertEqual(resolve_endpoint(env={}, worker_file=self.file).kind, "tcp")

    def test_explicit_token_wins(self):
        self.save({"host": "https://w.example.com", "token": "saved"})
        self.assertEqual(resolve_endpoint(env={}, worker_file=self.file, token="mine").token, "mine")


class FixesTest(Base):
    def test_second_write_stdin_raises(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/s/containers/c/stdin", lambda r, h: send_json(h, 200, {"ok": True}))
        self.assertEqual(self.c.write_stdin("s", "c", b"hi\n"), {"ok": True})
        with self.assertRaises(Kosa8Error):
            self.c.write_stdin("s", "c", b"again\n")
        self.assertEqual(len(self.d.find("POST", "/kosa8/v1/sandboxes/s/containers/c/stdin")), 1)

    def test_bad_frame_is_protocol_error(self):
        from kosa8 import Kosa8ProtocolError

        def bad(r, h):
            h.send_response(200)
            h.send_header("Content-Type", "application/x-ndjson")
            h.send_header("Content-Length", "9")
            h.end_headers()
            h.wfile.write(b"not json\n")

        self.d.on("POST", "/kosa8/v1/sandboxes/s/run", bad)
        with self.assertRaises(Kosa8ProtocolError):
            self.c.run("s", "alpine", ["true"])

    def test_checkpoint_retries_on_id_collision(self):
        calls = []

        def snap(r, h):
            calls.append(r.json()["id"])
            if len(calls) == 1:
                send_json(h, 409, {"error": "snapshot already exists: s-ckpt-1"})
            else:
                send_json(h, 201, {"id": r.json()["id"]})

        self.d.on("POST", "/kosa8/v1/sandboxes/s/snapshot", snap)
        self.d.on("GET", "/kosa8/v1/snapshots", lambda r, h: send_json(h, 200, [{"id": "s-ckpt-1"}]))
        out = self.c.checkpoint("s", label="a")
        self.assertEqual(calls, ["s-ckpt-1", "s-ckpt-2"])
        self.assertEqual(out["checkpoint"]["id"], "s-ckpt-2")

    def test_conflict_for_another_reason_is_raised(self):
        self.d.on("POST", "/kosa8/v1/sandboxes/s/snapshot", lambda r, h: send_json(h, 409, {"error": "sandbox busy"}))
        self.d.on("GET", "/kosa8/v1/snapshots", lambda r, h: send_json(h, 200, []))
        from kosa8 import Kosa8ConflictError

        with self.assertRaises(Kosa8ConflictError):
            self.c.checkpoint("s")

    @unittest.skipIf(os.name == "nt", "fork and flock are POSIX")
    def test_file_lock_times_out(self):
        import fcntl

        from kosa8 import Kosa8CheckpointError

        d = tempfile.mkdtemp()
        store = FileCheckpointStore(os.path.join(d, "checkpoints.json"), lock_timeout=0.3)
        fd = os.open(store.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        pid = os.fork()
        if pid == 0:  # hold the lock from another process, as the CLI would
            fcntl.flock(fd, fcntl.LOCK_EX)
            import time as _t

            _t.sleep(2)
            os._exit(0)
        try:
            import time as _t

            _t.sleep(0.3)
            with self.assertRaises(Kosa8CheckpointError):
                store.update(lambda data: None)
        finally:
            os.waitpid(pid, 0)
            os.close(fd)

    def test_sandbox_handle_has_stdin_methods(self):
        h = self.c.sandbox("s")
        for m in ("exec_frames", "start_interactive", "write_stdin", "attach"):
            self.assertTrue(callable(getattr(h, m)))
