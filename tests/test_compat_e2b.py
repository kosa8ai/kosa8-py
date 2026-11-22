import io
import unittest

from compat_fake import FakeCloud

import kosa8.compat as compat
from kosa8.compat import Unsupported
from kosa8.compat import e2b
from kosa8.compat.e2b import CommandExitException, FileType, Sandbox
from kosa8.compat.e2b_code_interpreter import Sandbox as CISandbox


class E2BTest(unittest.TestCase):
    def setUp(self):
        self.f = FakeCloud()
        compat.set_default_cloud(self.f.cloud())

    def tearDown(self):
        compat.set_default_cloud(None)

    def box(self, **kw):
        return Sandbox.create(**kw)

    # lifecycle ----------------------------------------------------------------

    def test_create_maps_arguments(self):
        sb = Sandbox.create("python", timeout=600, envs={"A": "1"}, metadata={"team": "ml"}, allow_internet_access=False)
        body = self.f.last("POST /v1/sandboxes")["body"]
        self.assertEqual(body, {"template": "python", "ttl_s": 600, "env": {"A": "1"}, "egress": {"internet": False},
                                "labels": {"team": "ml"}})
        self.assertFalse([p for m, p, _, _ in self.f.seen if m == "PUT" and p.endswith("/labels")])  # labels go in the create body
        self.assertTrue(sb.sandbox_id.startswith("sbx_"))
        self.assertIs(sb.kosa8.id, sb.sandbox_id)

    def test_create_defaults_and_minimum_ttl(self):
        # E2B's default image has Python, so the shim's default (and E2B's
        # "base") is kosa8's python template, not kosa8's Alpine base.
        Sandbox.create()
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"], {"template": "python", "ttl_s": 300})
        Sandbox.create("base", timeout=5)
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"], {"template": "python", "ttl_s": 60})
        Sandbox.create("node")
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"]["template"], "node")
        self.assertNotIn("persistent", self.f.last("POST /v1/sandboxes")["body"])

    def test_create_pause_on_timeout_and_from_snapshot(self):
        Sandbox.create(timeout=120, lifecycle={"on_timeout": "pause"})
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"], {"template": "python", "sleep_after_s": 120})
        Sandbox.create("snap_9")
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"]["snapshot"], "snap_9")

    def test_create_unsupported(self):
        for kw in ({"mcp": {"x": 1}}, {"network": {"allow_out": ["1.1.1.1"]}}, {"iam": {"tokens": {}}},
                   {"volume_mounts": {"/d": "v"}}, {"idempotency_key": "k"}, {"cpu_count": 2},
                   {"lifecycle": {"on_timeout": "pause", "auto_resume": True}}):
            with self.assertRaises(Unsupported) as c:
                Sandbox.create(**kw)
            self.assertIn("e2b Sandbox.create(", str(c.exception))
            self.assertIn("On kosa8:", str(c.exception))
        with self.assertRaisesRegex(Unsupported, "not a kosa8 address"):
            Sandbox.create(domain="e2b.app")
        self.assertNotIn("POST /v1/sandboxes", self.f.routes())

    def test_connect_class_and_instance_wakes(self):
        sid = self.f.add_box(state="sleeping")
        sb = Sandbox.connect(sid, timeout=900)
        self.assertEqual(sb.sandbox_id, sid)
        self.assertIn(f"POST /v1/sandboxes/{sid}/wake", self.f.routes())
        self.assertEqual(self.f.last(f"PATCH /v1/sandboxes/{sid}")["body"], {"ttl_s": 900})
        again = sb.connect()
        self.assertEqual(again.sandbox_id, sid)
        with self.assertRaises(Unsupported):
            Sandbox.connect(sid, on_resume="fresh")

    def test_kill_set_timeout_info_running(self):
        sb = self.box()
        sb.set_timeout(1200)
        self.assertEqual(self.f.last("PATCH")["body"], {"ttl_s": 1200})
        Sandbox.set_timeout(sb.sandbox_id, 30)
        self.assertEqual(self.f.last("PATCH")["body"], {"ttl_s": 60})
        info = sb.get_info()
        self.assertEqual((info.sandbox_id, info.state), (sb.sandbox_id, "running"))
        self.assertEqual(Sandbox.get_info(sb.sandbox_id).sandbox_id, sb.sandbox_id)
        self.assertTrue(sb.is_running())
        self.assertTrue(sb.kill())
        self.assertFalse(sb.is_running())
        self.assertFalse(Sandbox.kill(sb.sandbox_id))
        other = self.box()
        self.assertTrue(Sandbox.kill(other.sandbox_id))

    def test_context_manager_kills(self):
        with self.box() as sb:
            sid = sb.sandbox_id
        self.assertIn(f"DELETE /v1/sandboxes/{sid}", self.f.routes())

    def test_pause(self):
        sb = self.box()
        self.assertTrue(sb.pause())
        self.assertIn(f"POST /v1/sandboxes/{sb.sandbox_id}/sleep", self.f.routes())
        self.assertFalse(sb.pause())
        self.assertFalse(Sandbox.beta_pause(sb.sandbox_id))
        with self.assertRaises(Unsupported):
            sb.pause(keep_memory=False)

    def test_fork_and_snapshots(self):
        sb = self.box()
        a, b = sb.fork(count=2, timeout=600)
        self.assertEqual(self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/fork")["body"], {"n": 2})
        self.assertNotEqual(a.sandbox_id, b.sandbox_id)
        self.assertEqual(self.f.last("PATCH")["body"], {"ttl_s": 600})
        self.assertEqual(len(Sandbox.fork(sb.sandbox_id)), 1)
        snap = sb.create_snapshot("v1")
        self.assertTrue(snap.snapshot_id.startswith("snap_"))
        self.assertEqual(snap.sandbox_id, sb.sandbox_id)
        Sandbox.create_snapshot(sb.sandbox_id)
        page = sb.list_snapshots()
        self.assertIsNone(page.next_token)
        self.assertTrue(page.has_next)
        self.assertEqual(len(page.next_items()), 2)
        self.assertFalse(page.has_next)
        self.assertEqual([s.name for s in Sandbox.list_snapshots(name="v1").next_items()], ["v1"])
        self.assertTrue(Sandbox.delete_snapshot(snap.snapshot_id))
        self.assertFalse(Sandbox.delete_snapshot(snap.snapshot_id))
        with self.assertRaises(Unsupported):
            sb.create_snapshot(mode="filesystem")

    def test_list(self):
        a = self.box(metadata={"team": "ml"})
        b = self.box()
        b.pause()
        self.box().kill()
        ids = {i.sandbox_id for i in Sandbox.list().next_items()}
        self.assertEqual(ids, {a.sandbox_id, b.sandbox_id})
        self.assertEqual([i.sandbox_id for i in Sandbox.list({"metadata": {"team": "ml"}}).next_items()], [a.sandbox_id])
        self.assertEqual([i.state for i in Sandbox.list({"state": ["paused"]}).next_items()], ["paused"])

    # commands ---------------------------------------------------------------------

    def test_run(self):
        sb = self.box()
        seen = []
        r = sb.commands.run("echo hi", envs={"X": "1"}, cwd="src", on_stdout=seen.append)
        self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "ran echo hi\n", ""))
        self.assertEqual(seen, ["ran echo hi\n"])
        cmd = self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/exec")["body"]
        self.assertEqual(cmd["env"], ["X=1"])
        self.assertEqual(cmd["cmd"][:5], ["sh", "-c", 'cd "$1" || exit 1; shift; exec "$@"', "sh", "/workspace/src"])
        self.assertEqual(cmd["cmd"][-1], "echo hi")
        self.assertIn("command -v bash", cmd["cmd"][-3])
        sb.commands.run("ls", shell=["/bin/sh", "-c"])
        self.assertEqual(self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/exec")["body"]["cmd"], ["/bin/sh", "-c", "ls"])
        self.assertIsNone(sb.commands.run("x", retain_output=False).stdout)

    def test_run_nonzero_raises_command_exit(self):
        sb = self.box()
        self.f.on_exec = lambda argv, env: (2, "out", "boom")
        with self.assertRaises(CommandExitException) as c:
            sb.commands.run("false")
        self.assertEqual((c.exception.exit_code, c.exception.stderr, c.exception.stdout), (2, "boom", "out"))
        self.assertIsInstance(c.exception, e2b.SandboxException)

    def test_run_timeout_becomes_timeout_exception(self):
        sb = self.box()
        self.f.fail[f"POST /v1/sandboxes/{sb.sandbox_id}/exec"] = (504, {"error": "exec_timeout", "message": "too long"})
        with self.assertRaises(e2b.TimeoutException) as c:
            sb.commands.run("sleep 999")
        self.assertIn("too long", str(c.exception))

    def test_run_background_wait_kill_connect(self):
        sb = self.box()
        self.f.bg_polls_until_exit = 2
        h = sb.commands.run("make", background=True)
        self.assertTrue(h.pid.startswith("bg_"))
        got = []
        r = h.wait(on_stdout=got.append)
        self.assertEqual((r.exit_code, r.stdout, got), (0, "ran make\n", ["ran make\n"]))
        self.assertEqual(list(h), [("ran make\n", None, None)])
        h2 = sb.commands.run("serve", background=True)
        self.assertEqual(sb.commands.connect(h2.pid).pid, h2.pid)
        self.assertTrue(h2.kill())
        self.assertIn(f"POST /v1/sandboxes/{sb.sandbox_id}/exec/{h2.pid}/stop", self.f.routes())
        self.assertFalse(sb.commands.kill("bg_nope"))
        self.assertTrue(sb.commands.kill(h2.pid))
        h.disconnect()

    def test_background_nonzero(self):
        sb = self.box()
        self.f.on_exec = lambda argv, env: (3, "partial", "")
        with self.assertRaises(CommandExitException):
            sb.commands.run("x", background=True).wait()

    def test_commands_unsupported(self):
        sb = self.box()
        for call in (lambda: sb.commands.run("x", user="root"), lambda: sb.commands.run("x", stdin=True),
                     lambda: sb.commands.list(), lambda: sb.commands.send_stdin(1, "x"),
                     lambda: sb.commands.close_stdin(1), lambda: sb.commands.stream_input(1),
                     lambda: sb.commands.run("x", background=True).send_stdin("x"),
                     lambda: sb.commands.run("x", background=True).close_stdin(),
                     lambda: sb.commands.run("x", background=True).wait(on_pty=print)):
            with self.assertRaises(Unsupported):
                call()
        with self.assertRaises(e2b.InvalidArgumentException):
            sb.commands.run("x", shell="sh -c")

    # files ----------------------------------------------------------------------------

    def test_files(self):
        sb = self.box()
        info = sb.files.write("a.txt", "hello")
        self.assertEqual((info.path, info.name, info.type), ("/workspace/a.txt", "a.txt", FileType.FILE))
        self.assertEqual(self.f.last("PUT")["query"], {"path": "/workspace/a.txt"})
        sb.files.write("/workspace/b.bin", io.BytesIO(b"\x00\x01"))
        self.assertEqual(sb.files.read("a.txt"), "hello")
        self.assertEqual(sb.files.read("/workspace/b.bin", format="bytes"), bytearray(b"\x00\x01"))
        self.assertEqual(list(sb.files.read("a.txt", format="stream")), [b"hello"])
        with self.assertRaises(e2b.InvalidArgumentException):
            sb.files.read("a.txt", format="xml")
        with self.assertRaises(e2b.FileNotFoundException):
            sb.files.read("missing.txt")
        out = sb.files.write_files([{"path": "c.txt", "data": b"c"}, e2b.WriteEntry(path="d.txt", data="d")])
        self.assertEqual([w.name for w in out], ["c.txt", "d.txt"])

    def test_list_exists_info_dirs(self):
        sb = self.box()
        self.assertTrue(sb.files.make_dir("data"))
        self.assertFalse(sb.files.make_dir("data"))
        sb.files.write("data/x.csv", "1,2")
        sb.files.write("top.txt", "t")
        names = sorted(e.name for e in sb.files.list("/workspace"))
        self.assertEqual(names, ["data", "top.txt"])
        deep = sorted(e.path for e in sb.files.list("/workspace", depth=2))
        self.assertEqual(deep, ["/workspace/data", "/workspace/data/x.csv", "/workspace/top.txt"])
        self.assertTrue(sb.files.exists("data/x.csv"))
        self.assertFalse(sb.files.exists("nope"))
        gi = sb.files.get_info("top.txt")
        self.assertEqual((gi.type, gi.size, gi.path), (FileType.FILE, 1, "/workspace/top.txt"))
        self.assertEqual(sb.files.get_info("data").type, FileType.DIR)
        with self.assertRaises(e2b.FileNotFoundException):
            sb.files.get_info("nope")
        sb.files.remove("data")
        self.assertEqual(self.f.last("DELETE")["query"], {"path": "/workspace/data", "recursive": "1"})
        with self.assertRaises(e2b.InvalidArgumentException):
            sb.files.list("/workspace", depth=0)

    def test_rename(self):
        sb = self.box()
        sb.files.write("a.txt", "x")

        def mv(argv, env):
            if argv[0] == "mv":
                self.f.files[(sb.sandbox_id, argv[2])] = self.f.files.pop((sb.sandbox_id, argv[1]))
                return 0, "", ""
            return None

        self.f.on_exec = mv
        self.assertEqual(sb.files.rename("a.txt", "b.txt").path, "/workspace/b.txt")
        self.f.on_exec = lambda argv, env: (1, "", "mv: no such file")
        with self.assertRaisesRegex(e2b.SandboxException, "no such file"):
            sb.files.rename("zz", "yy")

    def test_files_unsupported(self):
        sb = self.box()
        for call in (lambda: sb.files.watch_dir("/workspace"), lambda: sb.files.read("a", user="root"),
                     lambda: sb.files.write("a", "x", metadata={"k": "v"}), lambda: sb.pty, lambda: sb.git,
                     lambda: sb.get_host(3000), lambda: sb.get_metrics(), lambda: sb.update_network({}),
                     lambda: sb.upload_url("a"), lambda: sb.get_mcp_token(), lambda: sb.get_mcp_url()):
            with self.assertRaises(Unsupported) as c:
                call()
            self.assertTrue(c.exception.alternative)

    def test_download_url(self):
        sb = self.box()
        self.assertIn("path=/workspace/a.txt", sb.download_url("a.txt"))
        self.assertEqual(self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/files/link")["body"],
                         {"path": "/workspace/a.txt", "method": "GET"})

    def test_errors_translate(self):
        with self.assertRaises(e2b.NotFoundException) as c:
            Sandbox.connect("sbx_missing")
        self.assertIsNotNone(c.exception.__cause__)

    def test_api_key_must_be_kosa8(self):
        compat.set_default_cloud(None)
        with self.assertRaisesRegex(Exception, "k8c_live_"):
            Sandbox.create(api_key="e2b_123")

    # code interpreter -------------------------------------------------------------------

    def test_code_interpreter_readme_example(self):
        # The README's first run_code works on a sandbox the shim created itself.
        with CISandbox.create() as sb:
            self.assertEqual(sb.run_code("6 * 7").error, None)
        snap = CISandbox.create("snap_1")
        self.assertNotIn("persistent", self.f.last("POST /v1/sandboxes")["body"])  # a snapshot keeps its own kind
        del snap

    def test_plain_e2b_run_code_on_non_persistent_is_a_clear_error(self):
        sb = Sandbox.create()
        with self.assertRaises(Exception) as c:
            sb.run_code("1")
        self.assertIn("persistent", str(c.exception))

    def test_code_interpreter(self):
        sb = CISandbox.create()
        body = self.f.last("POST /v1/sandboxes")["body"]
        self.assertEqual(body["template"], "python")
        self.assertIs(body["persistent"], True)  # the interpreter refuses a non-persistent sandbox (409)
        self.f.code_answer = {
            "execution_count": 4, "stdout": "a\nb\n", "stderr": "warn\n",
            "results": [{"type": "text", "data": "<display>"}, {"type": "image/png", "data": "iVBOR"},
                        {"type": "text", "data": "<Figure>"}, {"type": "html", "data": "<b>2</b>"},
                        {"type": "text", "data": "2"}, {"type": "image/png", "data": "Zm9v"}],
        }
        outs, results = [], []
        ex = sb.run_code("x", on_stdout=outs.append, on_result=results.append, timeout=20)
        self.assertEqual(self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/code")["body"], {"code": "x", "timeout_s": 20})
        self.assertEqual(ex.text, "2")
        self.assertEqual(ex.execution_count, 4)
        self.assertEqual([r.formats() for r in ex.results], [["text"], ["text", "png"], ["text", "html"], ["png"]])
        self.assertEqual(ex.results[2].html, "<b>2</b>")
        self.assertEqual(str(ex.results[2]), "2")
        self.assertIn(sb.sandbox_id, repr(sb))
        self.assertEqual(ex.logs.stdout, ["a\n", "b\n"])
        self.assertEqual([str(o) for o in outs], ["a\n", "b\n"])
        self.assertEqual(len(results), 4)
        self.assertIsNone(ex.error)
        self.f.code_answer = {"results": [], "stdout": "", "stderr": "",
                              "error": {"name": "ZeroDivisionError", "value": "division by zero", "traceback": "tb"}}
        errs = []
        ex = sb.run_code("1/0", on_error=errs.append)
        self.assertEqual((ex.error.name, ex.error.value), ("ZeroDivisionError", "division by zero"))
        self.assertIs(errs[0], ex.error)
        self.assertIsNone(ex.text)

    def test_code_contexts(self):
        sb = CISandbox.create(template="custom")
        self.assertEqual(self.f.last("POST /v1/sandboxes")["body"]["template"], "custom")
        ctx = sb.create_code_context()
        self.assertEqual((ctx.id, ctx.language), ("ctx-1", "python"))
        sb.run_code("y = 1", context=ctx)
        self.assertEqual(self.f.last("POST /v1/sandboxes/" + sb.sandbox_id + "/code")["body"]["context"], "ctx-1")
        self.assertEqual([c.id for c in sb.list_code_contexts()], ["default"])
        sb.restart_code_context(ctx)
        self.assertIn(f"POST /v1/sandboxes/{sb.sandbox_id}/code/contexts/ctx-1/reset", self.f.routes())
        sb.remove_code_context("ctx-1")
        self.assertIn(f"DELETE /v1/sandboxes/{sb.sandbox_id}/code/contexts/ctx-1", self.f.routes())

    def test_code_unsupported(self):
        sb = CISandbox.create()
        for call in (lambda: sb.run_code("x", language="javascript"), lambda: sb.run_code("x", envs={"A": "1"}),
                     lambda: sb.create_code_context(cwd="/tmp"), lambda: sb.create_code_context(language="r")):
            with self.assertRaises(Unsupported):
                call()
        with self.assertRaises(e2b.InvalidArgumentException):
            sb.run_code("x", language="python", context=e2b.Context("c", "python", "/"))


if __name__ == "__main__":
    unittest.main()
