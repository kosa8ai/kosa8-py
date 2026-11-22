import os
import tempfile
import unittest

from compat_fake import FakeCloud

import kosa8.compat as compat
from kosa8.compat import Unsupported
from kosa8.compat import daytona as dt
from kosa8.compat.daytona import (
    CodeRunParams,
    CreateSandboxFromImageParams,
    CreateSandboxFromSnapshotParams,
    Daytona,
    DaytonaConfig,
    FileUpload,
    ListSandboxesQuery,
    Resources,
)


class DaytonaTest(unittest.TestCase):
    def setUp(self):
        self.f = FakeCloud()
        compat.set_default_cloud(self.f.cloud())
        self.d = Daytona()

    def tearDown(self):
        compat.set_default_cloud(None)

    def created(self):
        return self.f.last("POST /v1/sandboxes")["body"]

    # client ------------------------------------------------------------------------------

    def test_create_default_is_python(self):
        sb = self.d.create()
        self.assertEqual(self.created(), {"template": "python"})
        self.assertEqual(sb.state, "started")
        self.assertEqual(sb.get_work_dir(), "/workspace")

    def test_create_from_snapshot_params(self):
        self.d.create(CreateSandboxFromSnapshotParams(
            snapshot="node", name="dev", env_vars={"DEBUG": "1"}, labels={"env": "dev"},
            auto_stop_interval=15, ttl_minutes=120, network_block_all=True, target="us", language="javascript",
        ), timeout=40)
        self.assertEqual(self.created(), {"template": "node", "sleep_after_s": 900, "ttl_s": 7200, "env": {"DEBUG": "1"},
                                          "name": "dev", "egress": {"internet": False}, "region": "us",
                                          "labels": {"env": "dev"}})
        self.assertFalse([p for m, p, _, _ in self.f.seen if m == "PUT" and p.endswith("/labels")])  # labels go in the create body
        self.d.create(CreateSandboxFromSnapshotParams(snapshot="snap_7", auto_pause_interval=5, auto_delete_interval=-1))
        self.assertEqual(self.created(), {"snapshot": "snap_7", "sleep_after_s": 300})

    def test_create_from_image(self):
        self.d.create(CreateSandboxFromImageParams(image="debian:12.9", resources=Resources(cpu=2, memory=4, disk=20)))
        self.assertEqual(self.created(), {"image": "debian:12.9", "cpus": 2, "mem_mb": 4096, "disk_gb": 20})

    def test_config_target_is_default_region(self):
        Daytona(DaytonaConfig(target="eu", api_url="https://api.kosa8.com")).create()
        self.assertEqual(self.created()["region"], "eu")

    def test_create_unsupported(self):
        for p in (CreateSandboxFromSnapshotParams(public=True), CreateSandboxFromSnapshotParams(volumes=["v"]),
                  CreateSandboxFromSnapshotParams(os_user="daytona"), CreateSandboxFromSnapshotParams(secrets={"A": "s"}),
                  CreateSandboxFromSnapshotParams(auto_delete_interval=0), CreateSandboxFromSnapshotParams(ephemeral=True),
                  CreateSandboxFromSnapshotParams(domain_allow_list="a.com"),
                  CreateSandboxFromImageParams(image=object()), CreateSandboxFromImageParams(image="x", resources=Resources(gpu=1)),
                  CreateSandboxFromImageParams(image="x", resources=Resources(cpu=2))):
            with self.assertRaises(Unsupported, msg=repr(p)) as c:
                self.d.create(p)
            self.assertIn("daytona", str(c.exception))
        self.assertNotIn("POST /v1/sandboxes", self.f.routes())

    def test_client_context_manager(self):
        with Daytona() as d:
            d.create()
        d.close()

    def test_config_refusals(self):
        with self.assertRaisesRegex(Unsupported, "not a kosa8 address"):
            Daytona(DaytonaConfig(api_url="https://app.daytona.io/api"))
        with self.assertRaises(Unsupported):
            Daytona(DaytonaConfig(jwt_token="jwt"))
        compat.set_default_cloud(None)
        with self.assertRaisesRegex(Exception, "k8c_live_"):
            Daytona(DaytonaConfig(api_key="dtn_123"))

    def test_get_list_delete_start_stop(self):
        sb = self.d.create(CreateSandboxFromSnapshotParams(name="web", labels={"env": "dev"}))
        self.d.create()
        self.assertEqual(self.d.get(sb.id).id, sb.id)
        self.assertEqual(self.d.get("web").id, sb.id)
        with self.assertRaises(dt.DaytonaNotFoundError):
            self.d.get("nope")
        self.assertEqual([s.id for s in self.d.list(ListSandboxesQuery(labels={"env": "dev"}))], [sb.id])
        self.assertEqual(len(list(self.d.list())), 2)
        self.d.stop(sb)
        self.assertEqual(sb.state, "stopped")
        self.assertEqual([s.id for s in self.d.list(ListSandboxesQuery(states=["stopped"]))], [sb.id])
        self.d.start(sb)
        self.assertEqual(sb.state, "started")
        self.assertIn(f"POST /v1/sandboxes/{sb.id}/wake", self.f.routes())
        self.d.delete(sb)
        self.assertIn(f"DELETE /v1/sandboxes/{sb.id}", self.f.routes())
        with self.assertRaises(dt.DaytonaNotFoundError) as c:
            self.d.get(sb.id)
        self.assertEqual(c.exception.status_code, 404)

    # sandbox ---------------------------------------------------------------------------------

    def test_sandbox_lifecycle_methods(self):
        sb = self.d.create(CreateSandboxFromImageParams(image="x", resources=Resources(cpu=2, memory=4)))
        self.assertEqual((sb.cpu, sb.memory, sb.disk, sb.target, sb.name), (2, 4, None, None, sb.id))
        self.assertIsNone(sb.created_at)
        self.assertEqual(sb.get_user_home_dir(), "/workspace")
        self.assertIn(sb.id, repr(sb))
        sb.set_autostop_interval(30)
        self.assertEqual(self.f.last("PATCH")["body"], {"sleep_after_s": 1800})
        sb.set_ttl(10)
        self.assertEqual(self.f.last("PATCH")["body"], {"ttl_s": 600})
        sb.set_ttl(0)
        self.assertEqual(self.f.last("PATCH")["body"], {"ttl_s": None})
        self.assertEqual(sb.set_labels({"a": "b"}), {"a": "b"})
        self.assertEqual(sb.labels, {"a": "b"})
        sb.set_auto_archive_interval(60)
        sb.pause()
        self.assertEqual(sb.state, "stopped")
        sb.archive()
        sb.start()
        sb.refresh_data()
        self.assertEqual(sb.get_preview_link(3000).url, "https://p3000.example/?t=sig")
        copy = sb.fork()
        self.assertNotEqual(copy.id, sb.id)
        sb.create_snapshot("golden")
        self.assertEqual(self.f.last(f"POST /v1/sandboxes/{sb.id}/snapshots")["body"], {"name": "golden"})
        sb.wait_for_sandbox_start()
        sb.delete()
        with self.assertRaises(dt.DaytonaError):
            sb.set_autostop_interval(-1)

    def test_sandbox_unsupported(self):
        sb = self.d.create()
        names = ["get_metrics", "get_metrics_latest", "create_lsp_server", "set_public", "create_signed_preview_url",
                 "expire_signed_preview_url", "update_network_settings", "update_secrets", "update_env", "resize",
                 "create_ssh_access", "revoke_ssh_access", "validate_ssh_access", "recover", "download_url", "upload_url",
                 "set_auto_delete_interval"]
        for n in names:
            with self.assertRaises(Unsupported) as c:
                getattr(sb, n)(1)
            self.assertIn(n, str(c.exception))
        for prop in ("git", "computer_use", "code_interpreter"):
            with self.assertRaises(Unsupported):
                getattr(sb, prop)
        with self.assertRaises(Unsupported):
            sb.fork(name="x")
        for prop in ("volume", "object_storage"):
            with self.assertRaises(Unsupported):
                getattr(self.d, prop)

    # process --------------------------------------------------------------------------------------

    def test_exec(self):
        sb = self.d.create()
        self.f.on_exec = lambda argv, env: (0, "Hello\n", "warn\n")
        r = sb.process.exec("echo 'Hello'", cwd="src", env={"A": "1"}, timeout=5)
        self.assertEqual((r.exit_code, r.result, r.artifacts.stdout), (0, "Hello\nwarn\n", "Hello\nwarn\n"))
        body = self.f.last(f"POST /v1/sandboxes/{sb.id}/exec")["body"]
        self.assertEqual(body["cmd"][4:], ["/workspace/src", "timeout", "5", "sh", "-c", "echo 'Hello'"])
        self.assertEqual(body["env"], ["A=1"])
        self.f.on_exec = lambda argv, env: (7, "", "bad\n")
        self.assertEqual(sb.process.exec("false").exit_code, 7)  # returned, not raised

    def test_exec_timeout_error(self):
        sb = self.d.create()
        self.f.fail[f"POST /v1/sandboxes/{sb.id}/exec"] = (504, {"error": "exec_timeout", "message": "too long"})
        with self.assertRaises(dt.DaytonaTimeoutError):
            sb.process.exec("sleep 500")

    def test_code_run(self):
        sb = self.d.create()
        r = sb.process.code_run("print(1)", params=CodeRunParams(argv=["a"], env={"K": "v"}), timeout=9)
        body = self.f.last(f"POST /v1/sandboxes/{sb.id}/exec")["body"]
        self.assertEqual(body["cmd"], ["timeout", "9", "python3", "-c", "print(1)", "a"])
        self.assertEqual(body["env"], ["K=v"])
        self.assertIsNone(r.artifacts.charts)
        js = self.d.create(CreateSandboxFromSnapshotParams(snapshot="node", language="javascript"))
        js.process.code_run("console.log(1)")
        self.assertEqual(self.f.last(f"POST /v1/sandboxes/{js.id}/exec")["body"]["cmd"], ["node", "-e", "console.log(1)"])
        ts = self.d.create(CreateSandboxFromSnapshotParams(language="typescript"))
        with self.assertRaises(Unsupported):
            ts.process.code_run("let x: number = 1")

    def test_process_unsupported(self):
        sb = self.d.create()
        for n in ("create_session", "get_session", "execute_session_command", "get_session_command_logs", "list_sessions",
                  "delete_session", "create_pty_session", "connect_pty_session", "kill_pty_session"):
            with self.assertRaises(Unsupported):
                getattr(sb.process, n)("s")

    # file system ---------------------------------------------------------------------------------------

    def test_fs_upload_download(self):
        sb = self.d.create()
        sb.fs.upload_file(b"Hello", "tmp/hello.txt")
        self.assertEqual(self.f.last("PUT")["query"], {"path": "/workspace/tmp/hello.txt"})
        self.assertEqual(sb.fs.download_file("tmp/hello.txt"), b"Hello")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "local.txt")
            with open(src, "wb") as fh:
                fh.write(b"from disk")
            sb.fs.upload_file(src, "/workspace/disk.txt")
            out = os.path.join(d, "sub", "copy.txt")
            self.assertIsNone(sb.fs.download_file("/workspace/disk.txt", out))
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"from disk")
        sb.fs.upload_files([FileUpload(source=b"1", destination="a.txt"), FileUpload(source=b"2", destination="b.txt")])
        self.assertEqual(sb.fs.download_file("b.txt", 60), b"2")
        with self.assertRaises(dt.DaytonaFileNotFoundError):
            sb.fs.download_file("missing")

    def test_fs_listing_and_folders(self):
        sb = self.d.create()
        sb.fs.create_folder("data", "755")
        sb.fs.upload_file(b"abc", "data/f.txt")
        files = sb.fs.list_files("data")
        self.assertEqual([(i.name, i.is_dir, i.size) for i in files], [("f.txt", False, 3)])
        self.assertTrue(sb.fs.get_file_info("data").is_dir)
        self.assertEqual(sb.fs.get_file_info("data/f.txt").size, 3)
        with self.assertRaises(dt.DaytonaFileNotFoundError):
            sb.fs.get_file_info("nope")
        cmds = []
        self.f.on_exec = lambda argv, env: (cmds.append(argv), (0, "", ""))[1]
        sb.fs.create_folder("secret", "700")
        sb.fs.set_file_permissions("data/f.txt", mode="644", owner="me", group="g")
        sb.fs.move_files("data/f.txt", "data/g.txt")
        self.assertEqual(cmds, [["chmod", "700", "/workspace/secret"], ["chmod", "644", "/workspace/data/f.txt"],
                                ["chown", "me:g", "/workspace/data/f.txt"], ["mv", "/workspace/data/f.txt", "/workspace/data/g.txt"]])
        self.f.on_exec = lambda argv, env: (1, "", "denied")
        with self.assertRaisesRegex(dt.DaytonaError, "denied"):
            sb.fs.move_files("a", "b")
        sb.fs.delete_file("data", recursive=True)
        self.assertEqual(self.f.last("DELETE")["query"], {"path": "/workspace/data", "recursive": "1"})

    def test_fs_unsupported(self):
        sb = self.d.create()
        for call in (lambda: sb.fs.find_files("/", "x"), lambda: sb.fs.search_files("/", "*.py"),
                     lambda: sb.fs.replace_in_files(["a"], "x", "y"), lambda: sb.fs.download_file_stream("a"),
                     lambda: sb.fs.upload_file_stream("a", "b"), lambda: sb.fs.download_files([]),
                     lambda: sb.fs.list_files("/", depth=3)):
            with self.assertRaises(Unsupported):
                call()

    # snapshots ----------------------------------------------------------------------------------------

    def test_snapshot_service(self):
        sb = self.d.create()
        sb.create_snapshot("one")
        sb.create_snapshot("two")
        page = self.d.snapshot.list()
        self.assertEqual(([s.name for s in page.items], page.total, page.total_pages), (["one", "two"], 2, 1))
        p2 = self.d.snapshot.list(page=2, limit=1)
        self.assertEqual(([s.name for s in p2.items], p2.page, p2.total_pages), (["two"], 2, 2))
        self.assertEqual(len(self.d.snapshot.list(source_sandbox_id=sb.id).items), 2)
        one = self.d.snapshot.get("one")
        self.assertEqual(self.d.snapshot.get(one.id).name, "one")
        self.d.snapshot.delete("two")
        self.d.snapshot.delete(one)
        self.assertEqual(self.d.snapshot.list().items, [])
        with self.assertRaises(dt.DaytonaNotFoundError):
            self.d.snapshot.get("gone")
        with self.assertRaises(Unsupported):
            self.d.snapshot.create(object())
        with self.assertRaises(Unsupported):
            self.d.snapshot.activate("x")


if __name__ == "__main__":
    unittest.main()
