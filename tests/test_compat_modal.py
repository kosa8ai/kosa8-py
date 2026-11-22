import contextlib
import io
import os
import tempfile
import unittest

from compat_fake import FakeCloud

import kosa8.compat as compat
import kosa8.compat.modal as modal
from kosa8.compat import Unsupported


class ModalTest(unittest.TestCase):
    def setUp(self):
        self.f = FakeCloud()
        compat.set_default_cloud(self.f.cloud())
        self.app = modal.App.lookup("my-app", create_if_missing=True)

    def tearDown(self):
        compat.set_default_cloud(None)

    def created(self):
        return self.f.last("POST /v1/sandboxes")["body"]

    def test_create_maps_arguments(self):
        sb = modal.Sandbox.create(
            app=self.app, name="worker", image=modal.Image.debian_slim(python_version="3.11"),
            env={"A": "1", "DROP": None}, secrets=[modal.Secret.from_dict({"TOKEN": "t"}), modal.Secret.from_name("db")],
            timeout=1800, idle_timeout=120, cpu=1.5, memory=2048, block_network=True, region="us", tags={"team": "ml"},
        )
        self.assertEqual(self.created(), {
            "ttl_s": 1800, "image": "python:3.11-slim", "sleep_after_s": 120, "env": {"A": "1", "TOKEN": "t"},
            "secrets": ["db"], "cpus": 2, "mem_mb": 2048, "egress": {"internet": False}, "region": "us", "name": "worker",
            "labels": {"team": "ml", "modal-app": "my-app"},
        })
        self.assertFalse([p for m, p, _, _ in self.f.seen if m == "PUT" and p.endswith("/labels")])  # labels go in the create body
        self.assertTrue(sb.object_id.startswith("sbx_"))
        self.assertIsNone(sb.returncode)

    def test_create_defaults_images(self):
        modal.Sandbox.create(app=self.app)
        self.assertEqual(self.created(), {"ttl_s": 300, "template": "python", "labels": {"modal-app": "my-app"}})
        modal.Sandbox.create(image=modal.Image.from_registry("node:22"), timeout=10, region=["eu"])
        self.assertEqual(self.created(), {"ttl_s": 60, "image": "node:22", "region": "eu"})
        modal.Sandbox.create(image=modal.Image.from_id("snap_3"))
        self.assertEqual(self.created()["snapshot"], "snap_3")
        modal.Sandbox.create(image=modal.Image.from_id("universal"), cpu=(1, 2), memory=(512, 1024))
        self.assertEqual(self.created(), {"ttl_s": 300, "template": "universal", "cpus": 2, "mem_mb": 1024})

    def test_create_unsupported(self):
        for kw in ({"gpu": "A100"}, {"volumes": {"/v": "vol"}}, {"encrypted_ports": [8080]}, {"pty": True},
                   {"outbound_domain_allowlist": ["pypi.org"]}, {"readiness_probe": object()}, {"cloud": "aws"},
                   {"cpu": 2}, {"region": ["us", "eu"]}, {"custom_domain": "x.dev"}):
            with self.assertRaises(Unsupported, msg=str(kw)) as c:
                modal.Sandbox.create(app=self.app, **kw)
            self.assertIn("modal Sandbox.create", str(c.exception))
        with self.assertRaises(TypeError):
            modal.Sandbox.create(not_a_param=1)
        self.assertNotIn("POST /v1/sandboxes", self.f.routes())

    def test_secret_from_local_environ_and_reprs(self):
        os.environ["KOSA8_COMPAT_T"] = "v"
        try:
            self.assertEqual(modal.Secret.from_local_environ(["KOSA8_COMPAT_T", "NOT_SET_X"]).env, {"KOSA8_COMPAT_T": "v"})
        finally:
            del os.environ["KOSA8_COMPAT_T"]
        self.assertIn("my-app", repr(self.app))
        self.assertIn("node:22", repr(modal.Image.from_registry("node:22")))
        sb = modal.Sandbox.create(app=self.app)
        self.assertIn(sb.object_id, repr(sb))

    def test_image_builds_unsupported(self):
        img = modal.Image.debian_slim()
        for call in (lambda: img.pip_install("numpy"), lambda: img.apt_install("git"), lambda: img.run_commands("ls"),
                     lambda: img.add_local_dir(".", "/app"), lambda: img.env({"A": "1"}), lambda: img.workdir("/app"),
                     lambda: modal.Image.from_dockerfile("Dockerfile"), lambda: modal.Image.from_registry("x", secret=object()),
                     lambda: modal.Image.from_registry("x", add_python="3.12"), lambda: modal.Secret.from_dotenv()):
            with self.assertRaises(Unsupported) as c:
                call()
            self.assertIn("On kosa8:", str(c.exception))

    def test_exec(self):
        sb = modal.Sandbox.create(app=self.app, workdir="/app")
        self.f.on_exec = lambda argv, env: (0, "foo 1\nfoo 2\n", "w\n")
        p = sb.exec("bash", "-c", "for i in 1 2; do echo foo $i; done", env={"X": "1"},
                    secrets=[modal.Secret.from_dict({"S": "2"})], timeout=30)
        self.assertEqual(list(p.stdout), ["foo 1\n", "foo 2\n"])
        self.assertEqual(p.stdout.read(), "foo 1\nfoo 2\n")
        self.assertEqual(p.stderr.read(), "w\n")
        self.assertEqual((p.wait(), p.poll(), p.returncode), (0, 0, 0))
        body = self.f.last(f"POST /v1/sandboxes/{sb.object_id}/exec")["body"]
        self.assertEqual(body["cmd"][4:7], ["/app", "timeout", "30"])
        self.assertEqual(body["cmd"][-3:], ["bash", "-c", "for i in 1 2; do echo foo $i; done"])
        self.assertEqual(sorted(body["env"]), ["S=2", "X=1"])
        self.assertEqual(p.stdout.file_descriptor, 1)

    def test_exec_stream_types_bytes_and_timeout(self):
        sb = modal.Sandbox.create(app=self.app)
        self.f.on_exec = lambda argv, env: (0, "out\n", "err\n")
        p = sb.exec("x", text=False)
        self.assertEqual(p.stdout.read(), b"out\n")
        self.assertEqual(list(p.stderr), [b"err\n"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p = sb.exec("x", stdout=modal.StreamType.STDOUT, stderr=modal.StreamType.DEVNULL)
        self.assertEqual(buf.getvalue(), "out\n")
        self.assertEqual((p.stdout.read(), p.stderr.read()), ("", ""))
        self.f.on_exec = lambda argv, env: (124, "", "")
        with self.assertRaises(modal.ExecTimeoutError):
            sb.exec("sleep", "60", timeout=1)
        self.f.on_exec = lambda argv, env: (3, "", "")
        self.assertEqual(sb.exec("false").returncode, 3)

    def test_exec_errors_and_unsupported(self):
        sb = modal.Sandbox.create(app=self.app)
        self.f.fail[f"POST /v1/sandboxes/{sb.object_id}/exec"] = (504, {"error": "exec_timeout", "message": "slow"})
        with self.assertRaises(modal.exception.ExecTimeoutError):
            sb.exec("sleep", "999")
        del self.f.fail[f"POST /v1/sandboxes/{sb.object_id}/exec"]
        with self.assertRaises(Unsupported):
            sb.exec("bash", pty=True)
        with self.assertRaises(Unsupported):
            sb.exec("x", secrets=[modal.Secret.from_name("db")])
        with self.assertRaises(modal.InvalidError):
            sb.exec()
        p = sb.exec("cat")
        for call in (lambda: p.stdin.write(b"x"), lambda: p.stdin.write_eof(), lambda: p.stdin.drain(),
                     lambda: sb.stdin.write(b"x")):
            with self.assertRaises(Unsupported):
                call()

    def test_entrypoint_wait_stdout_stderr(self):
        def run(argv, env):
            if argv[0] == "cat":
                return 0, "entry err\n", ""
            return 0, "line 1\nline 2\n", ""

        self.f.on_exec = run
        self.f.bg_polls_until_exit = 2
        sb = modal.Sandbox.create("python", "-m", "http.server", app=self.app, workdir="/srv")
        bg = self.f.last(f"POST /v1/sandboxes/{sb.object_id}/exec")["body"]
        self.assertTrue(bg["background"])
        self.assertEqual(bg["cmd"][4], "/srv")
        self.assertEqual(bg["cmd"][-3:], ["python", "-m", "http.server"])
        err_path = bg["cmd"][-4]
        self.assertTrue(err_path.startswith("/workspace/.kosa8-modal/"))
        self.assertIsNone(sb.poll())  # first poll: still running
        sb.wait()
        self.assertEqual(sb.returncode, 0)
        self.assertEqual(sb.poll(), 0)
        self.assertEqual(sb.stdout.read(), "line 1\nline 2\n")
        self.assertEqual(list(sb.stdout), ["line 1\n", "line 2\n"])
        self.assertEqual(sb.stderr.read(), "entry err\n")
        self.assertEqual(self.f.last(f"POST /v1/sandboxes/{sb.object_id}/exec")["body"]["cmd"], ["cat", err_path])
        self.assertEqual(sb.terminate(wait=True), 0)
        self.assertIn(f"DELETE /v1/sandboxes/{sb.object_id}", self.f.routes())

    def test_no_entrypoint_poll_wait_terminate(self):
        sb = modal.Sandbox.create(app=self.app)
        self.assertIsNone(sb.poll())
        with self.assertRaises(Unsupported):
            sb.stdout.read()
        with self.assertRaises(Unsupported):
            sb.stderr.read()
        self.assertIsNone(sb.terminate())
        self.assertEqual(sb.poll(), 0)
        with self.assertRaises(modal.SandboxTerminatedError):
            sb.wait()
        sb.wait(raise_on_termination=False)
        sb.terminate()  # already gone: a no-op, as in Modal

    def test_from_id_list_tags(self):
        a = modal.Sandbox.create(app=self.app, tags={"k": "v"}, name="one")
        modal.Sandbox.create(app=modal.App("other"))
        self.assertEqual(modal.Sandbox.from_id(a.object_id).object_id, a.object_id)
        with self.assertRaises(modal.exception.NotFoundError):
            modal.Sandbox.from_id("sbx_nope")
        self.assertEqual([s.object_id for s in modal.Sandbox.list(app_id="my-app")], [a.object_id])
        self.assertEqual([s.object_id for s in modal.Sandbox.list(tags={"k": "v"})], [a.object_id])
        self.assertEqual(len(list(modal.Sandbox.list())), 2)
        self.assertEqual(modal.Sandbox.from_name("my-app", "one").object_id, a.object_id)
        with self.assertRaises(modal.NotFoundError):
            modal.Sandbox.from_name("my-app", "two")
        a.set_tags({"x": "y"})
        self.assertEqual(self.f.last("PUT")["body"], {"labels": {"modal-app": "my-app", "x": "y"}})
        self.assertEqual(a.get_tags(), {"x": "y"})

    def test_snapshot_filesystem(self):
        sb = modal.Sandbox.create(app=self.app)
        img = sb.snapshot_filesystem()
        self.assertTrue(img.object_id.startswith("snap_"))
        modal.Sandbox.create(app=self.app, image=img)
        self.assertEqual(self.created(), {"ttl_s": 300, "snapshot": img.object_id, "labels": {"modal-app": "my-app"}})

    def test_filesystem(self):
        sb = modal.Sandbox.create(app=self.app)
        fs = sb.filesystem
        fs.write_text("Hello\n", "/workspace/hello.txt")
        fs.write_bytes(b"\x01", "/workspace/b.bin")
        self.assertEqual(fs.read_text("/workspace/hello.txt"), "Hello\n")
        self.assertEqual(fs.read_bytes("/workspace/b.bin"), b"\x01")
        fs.make_directory("/workspace/a/b")
        entries = sorted((e.name, e.type) for e in fs.list_files("/workspace"))
        self.assertEqual(entries, [("b.bin", "file"), ("hello.txt", "file")])
        self.assertIn(("b", "directory"), [(e.name, e.type) for e in fs.list_files("/workspace/a")])
        st = fs.stat("/workspace/hello.txt")
        self.assertEqual((st.size, st.type, st.path), (6, "file", "/workspace/hello.txt"))
        with self.assertRaises(modal.SandboxFilesystemNotFoundError):
            fs.stat("/workspace/none")
        with self.assertRaises(modal.SandboxFilesystemNotFoundError):
            fs.read_text("/workspace/none")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.txt")
            with open(src, "w") as fh:
                fh.write("local")
            fs.copy_from_local(src, "/workspace/in.txt")
            dst = os.path.join(d, "out", "copy.txt")
            fs.copy_to_local("/workspace/in.txt", dst)
            with open(dst) as fh:
                self.assertEqual(fh.read(), "local")
        fs.remove("/workspace/a", recursive=True)
        self.assertEqual(self.f.last("DELETE")["query"], {"path": "/workspace/a", "recursive": "1"})
        with self.assertRaises(Unsupported):
            fs.make_directory("/workspace/x", create_parents=False)
        with self.assertRaises(Unsupported):
            fs.watch("/workspace")

    def test_sandbox_unsupported(self):
        sb = modal.Sandbox.create(app=self.app)
        for n in ("tunnels", "create_connect_token", "wait_until_ready", "snapshot_directory", "mount_image",
                  "unmount_image", "reload_volumes", "open", "ls", "mkdir", "rm", "watch"):
            with self.assertRaises(Unsupported) as c:
                getattr(sb, n)("/x")
            self.assertIn(n, str(c.exception))
        with self.assertRaises(Unsupported):
            sb.logs

    def test_errors(self):
        self.f.fail["POST /v1/sandboxes"] = (401, {"error": "unauthorized", "message": "bad key"})
        with self.assertRaises(modal.exception.AuthError):
            modal.Sandbox.create(app=self.app)


if __name__ == "__main__":
    unittest.main()
