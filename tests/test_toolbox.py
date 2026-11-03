import base64
import io
import json
import os
import tarfile
import tempfile
import unittest

from kosa8 import CodeResult, Kosa8Client, Kosa8NotFoundError, Kosa8StreamError, MemoryCheckpointStore
from kosa8.integrations import DEFAULT_TOOLS, sandbox_functions

from mock_daemon import MockDaemon, send_frames, send_json

S = "/kosa8/v1/sandboxes/sb1"


def _w(path, text):
    with open(path, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = MockDaemon().start()
        self.c = Kosa8Client(**self.d.client_args, env={}, checkpoint_store=MemoryCheckpointStore())

    def tearDown(self):
        self.d.stop()

    def last(self):
        return self.d.requests[-1]


class CodeTest(Base):
    def test_run_code_decodes_artifacts(self):
        png = b"\x89PNG fake"
        self.d.on("POST", S + "/code", lambda r, h: send_json(h, 200, {
            "language": "python", "image": "python:3.12-slim", "exit": 0, "stdout": "hi\n", "stderr": "",
            "artifacts": [{"type": "image/png", "name": "figure-1.png", "data": base64.b64encode(png).decode()}],
            "duration_ms": 12}))
        r = self.c.run_code("sb1", "print('hi')", env={"A": "1"}, timeout_s=10)
        self.assertIsInstance(r, CodeResult)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, "hi\n")
        self.assertEqual(r.artifacts[0].data, png)
        self.assertEqual(self.last().json(), {"language": "python", "code": "print('hi')", "env": {"A": "1"}, "timeout_s": 10})

    def test_timed_out_is_not_ok(self):
        self.d.on("POST", S + "/code", lambda r, h: send_json(h, 200, {"exit": 124, "stdout": "", "stderr": "killed", "timed_out": True}))
        r = self.c.run_code("sb1", "while True: pass")
        self.assertFalse(r.ok)
        self.assertTrue(r.timed_out)


class FilesTest(Base):
    def test_list_read_write_delete(self):
        self.d.on("GET", S + "/files", lambda r, h: send_json(h, 200, {"path": "/workspace", "files": [{"name": "a.txt", "type": "file", "size": 3}]}))

        def content(r, h):
            h.send_response(200)
            h.send_header("Content-Length", "5")
            h.end_headers()
            h.wfile.write(b"hello")

        self.d.on("GET", S + "/files/content", content)
        self.d.on("PUT", S + "/files/content", lambda r, h: send_json(h, 200, {"path": "/workspace/b.txt", "size": len(r.body)}))
        self.d.on("DELETE", S + "/files", lambda r, h: send_json(h, 200, {"removed": "/workspace/d"}))

        self.assertEqual(self.c.list_files("sb1")[0]["name"], "a.txt")
        self.assertIn("path=%2Fworkspace", self.last().url)
        self.assertEqual(self.c.read_text("sb1", "a.txt"), "hello")
        self.assertEqual(self.c.write_file("sb1", "b.txt", "héllo", mode="0644")["size"], 6)
        self.assertEqual(self.last().body, "héllo".encode())
        self.assertIn("mode=0644", self.last().url)
        self.c.delete_file("sb1", "d", recursive=True)
        self.assertIn("recursive=1", self.last().url)

    def test_read_missing_file_is_typed(self):
        self.d.on("GET", S + "/files/content", lambda r, h: send_json(h, 404, {"error": "no such file", "remediation": ""}))
        with self.assertRaises(Kosa8NotFoundError):
            self.c.read_file("sb1", "nope")

    def test_short_read_is_an_error(self):
        def content(r, h):
            h.send_response(200)
            h.send_header("Content-Length", "10")
            h.end_headers()
            h.wfile.write(b"abc")
            h.close_connection = True

        self.d.on("GET", S + "/files/content", content)
        with self.assertRaises(Exception):
            self.c.read_file("sb1", "x")


class GitSessionTest(Base):
    def test_git_commit_adds_then_commits(self):
        self.d.on("POST", S + "/git", lambda r, h: send_json(h, 200, {"op": r.json()["op"], "ok": True, "exit": 0, "stdout": "", "stderr": ""}))
        r = self.c.git_commit("sb1", "repo", "msg")
        self.assertTrue(r.ok)
        ops = [q.json()["op"] for q in self.d.requests]
        self.assertEqual(ops, ["add", "commit"])
        self.assertEqual(self.last().json(), {"op": "commit", "path": "repo", "message": "msg"})

    def test_shell_makes_the_session_on_first_use(self):
        made = []

        def exe(r, h):
            if not made:
                send_json(h, 404, {"error": "no such session: main", "remediation": "create it"})
            else:
                send_json(h, 200, {"exit": 0, "stdout": "/tmp\n", "stderr": "", "cwd": "/tmp"})

        def create(r, h):
            made.append(r.json())
            send_json(h, 200, {"name": "main", "id": "session-main", "status": "running"})

        self.d.on("POST", S + "/sessions/main/exec", exe)
        self.d.on("POST", S + "/sessions", create)
        r = self.c.shell("sb1", "cd /tmp && pwd")
        self.assertEqual(r.cwd, "/tmp")
        self.assertEqual(made, [{"name": "main"}])

    def test_shell_other_404_raises(self):
        self.d.on("POST", S + "/sessions/main/exec", lambda r, h: send_json(h, 404, {"error": "no such sandbox: sb1"}))
        with self.assertRaises(Kosa8NotFoundError):
            self.c.shell("sb1", "ls")


class LifecycleTemplateTest(Base):
    def test_create_sandbox_lifecycle_fields(self):
        self.d.on("POST", "/kosa8/v1/sandboxes", lambda r, h: send_json(h, 200, {"id": "x", "name": "x"}))
        self.c.create_sandbox(name="x", auto_stop_min=15, ttl_min=60, ephemeral=True, template="py")
        b = self.last().json()
        self.assertEqual((b["auto_stop_min"], b["ttl_min"], b["ephemeral"], b["template"]), (15, 60, True, "py"))
        self.assertNotIn("auto_delete_min", b)

    def test_set_lifecycle_sends_only_given_fields(self):
        self.d.on("POST", S + "/lifecycle", lambda r, h: send_json(h, 200, {"auto_stop_min": 0}))
        self.c.set_lifecycle("sb1", auto_stop_min=0)
        self.assertEqual(self.last().json(), {"auto_stop_min": 0})

    def test_build_template_streams_and_honours_dockerignore(self):
        ctx = tempfile.mkdtemp()
        _w(os.path.join(ctx, "Dockerfile"), "FROM alpine\n")
        _w(os.path.join(ctx, ".dockerignore"), "secret\n")
        os.mkdir(os.path.join(ctx, "secret"))
        _w(os.path.join(ctx, "secret", "k"), "x")
        _w(os.path.join(ctx, "app.py"), "print(1)")
        self.d.on("POST", "/kosa8/v1/templates", lambda r, h: send_frames(h, [
            {"stream": "status", "data": b"step 1\n"},
            {"done": True, "ok": True, "image": "kosa8-template/py:latest"}]))
        seen = []
        out = self.c.build_template("py", ctx, build_args={"V": "1"}, on_output=seen.append)
        self.assertEqual(out["image"], "kosa8-template/py:latest")
        self.assertEqual(seen, [b"step 1\n"])
        self.assertIn("buildarg=V%3D1", self.last().url)
        names = tarfile.open(fileobj=io.BytesIO(self.last().body)).getnames()
        self.assertIn("app.py", names)
        self.assertFalse([n for n in names if n.startswith("secret")])

    def test_build_template_error_frame_raises(self):
        ctx = tempfile.mkdtemp()
        _w(os.path.join(ctx, "Dockerfile"), "FROM alpine\n")
        self.d.on("POST", "/kosa8/v1/templates", lambda r, h: send_frames(h, [{"done": True, "error": "RUN failed"}]))
        with self.assertRaises(Kosa8StreamError):
            self.c.build_template("py", ctx)


class FunctionsTest(Base):
    def test_errors_come_back_as_text(self):
        self.d.on("POST", S + "/code", lambda r, h: send_json(h, 403, {"error": "egress denied", "remediation": "allow the host"}))
        fns = {f.__name__: f for f in sandbox_functions(self.c, "sb1")}
        self.assertEqual(sorted(fns), sorted(DEFAULT_TOOLS))
        out = fns["run_code"]("import requests")
        self.assertTrue(out.startswith("error: egress denied"))
        self.assertIn("what to do: allow the host", out)

    def test_subset_and_unknown(self):
        self.assertEqual([f.__name__ for f in sandbox_functions(self.c, "sb1", tools=["shell"])], ["shell"])
        with self.assertRaises(ValueError):
            sandbox_functions(self.c, "sb1", tools=["rm_rf"])

    def test_list_files_marks_dirs(self):
        self.d.on("GET", S + "/files", lambda r, h: send_json(h, 200, {"files": [
            {"name": "src", "type": "dir"}, {"name": "a", "type": "file", "size": 2}]}))
        (ls,) = sandbox_functions(self.c, "sb1", tools=["list_files"])
        self.assertEqual(ls(), "src/\na  2 bytes")


try:
    import agents  # noqa: F401
    HAVE_AGENTS = True
except ImportError:
    HAVE_AGENTS = False
try:
    import langchain_core  # noqa: F401
    HAVE_LC = True
except ImportError:
    HAVE_LC = False


@unittest.skipUnless(HAVE_AGENTS, "openai-agents not installed")
class OpenAIAgentsTest(Base):
    def test_tools_have_schemas(self):
        from kosa8.integrations.openai_agents import sandbox_tools

        tools = sandbox_tools(self.c, "sb1")
        by = {t.name: t for t in tools}
        self.assertIn("sandbox_shell", by)
        schema = by["sandbox_run_code"].params_json_schema
        self.assertIn("code", schema["properties"])
        self.assertIn("code", schema["required"])
        self.assertIn("python, javascript or typescript", schema["properties"]["language"]["description"])
        self.assertIn("Run a snippet", by["sandbox_run_code"].description)


@unittest.skipUnless(HAVE_LC, "langchain-core not installed")
class LangChainTest(Base):
    def test_tools_invoke(self):
        from kosa8.integrations.langchain import sandbox_tools

        self.d.on("POST", S + "/code", lambda r, h: send_json(h, 200, {"exit": 0, "stdout": "2\n", "stderr": ""}))
        tools = {t.name: t for t in sandbox_tools(self.c, "sb1")}
        self.assertIn("code", tools["sandbox_run_code"].args)
        out = tools["sandbox_run_code"].invoke({"code": "print(1+1)"})
        self.assertIn("stdout:\n2", out)
        self.assertEqual(self.last().json()["code"], "print(1+1)")


if __name__ == "__main__":
    unittest.main()
