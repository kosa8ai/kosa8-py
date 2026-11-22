"""Streamed exec (POST .../exec {stream: true}) in the cloud client and the
compat shims: output reaches callbacks as frames arrive, not at the end."""

import asyncio
import base64
import json
import unittest

from compat_fake import FakeCloud

import kosa8.compat as compat
from kosa8 import Kosa8Cloud, Kosa8ExecTimeoutError, Kosa8StreamError, CloudSandbox
from kosa8.errors import Kosa8BadRequestError
from kosa8.compat import daytona, e2b, modal

KEY = "k8c_live_testkey"


def frame(stream=None, data=None, **kw):
    f = dict(kw)
    if stream:
        f["stream"] = stream
        f["data"] = base64.b64encode(data if isinstance(data, bytes) else data.encode()).decode()
    return json.dumps(f).encode() + b"\n"


class StreamOpener:
    """A stream_opener whose lines are produced one by one, logging when each
    is handed over, so a test can see callbacks interleave with arrival."""

    def __init__(self, lines, status=200, refusal=None):
        self.lines = lines
        self.status = status
        self.refusal = refusal
        self.log = []
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        self.seen.append({"method": method, "url": url, "headers": headers, "body": json.loads(body)})
        if self.status >= 400:
            return self.status, json.dumps(self.refusal).encode(), {}

        def gen():
            for i, line in enumerate(self.lines):
                self.log.append(f"line{i}")
                yield line

        return self.status, gen(), {}


def box_with(opener):
    return CloudSandbox(Kosa8Cloud(KEY, env={}, base_url="http://localhost:8787", stream_opener=opener),
                        {"id": "sbx_1", "state": "running"})


class CoreStreamTest(unittest.TestCase):
    def test_callbacks_fire_as_frames_arrive(self):
        op = StreamOpener([frame("stdout", "1\n"), frame("stdout", "", heartbeat=True), frame("stderr", "warn\n"),
                           frame("stdout", "2\n"), frame(done=True, exit=0)])
        box = box_with(op)
        r = box.exec("for i in 1 2; do echo $i; done", on_stdout=lambda t: op.log.append(f"out:{t}"),
                      on_stderr=lambda t: op.log.append(f"err:{t}"), timeout_s=300)
        self.assertEqual(op.log, ["line0", "out:1\n", "line1", "line2", "err:warn\n", "line3", "out:2\n", "line4"])
        self.assertEqual((r.exit_code, r.stdout, r.stderr, r.failure_class), (0, "1\n2\n", "warn\n", "ok"))
        body = op.seen[0]["body"]
        self.assertEqual((body["stream"], body["timeout_s"], body["cmd"][:2]), (True, 300, ["sh", "-c"]))
        self.assertIn("application/x-ndjson", op.seen[0]["headers"]["Accept"])

    def test_iterate_and_utf8_split_across_frames(self):
        snow = "☃".encode()
        op = StreamOpener([frame("stdout", snow[:1]), frame("stdout", snow[1:] + b"!"), frame(done=True, exit=3)])
        s = box_with(op).exec_stream(["x"])
        self.assertEqual(list(s), [("stdout", "☃!")])
        self.assertEqual((s.exit_code, s.stdout), (3, "☃!"))

    def test_timeout_frame_raises_exec_timeout_with_output(self):
        op = StreamOpener([frame("stdout", "part"), frame(done=True, error="ran past timeout_s", code="exec_timeout")])
        with self.assertRaises(Kosa8ExecTimeoutError) as c:
            box_with(op).exec_stream("sleep 9", timeout_s=2).result()
        self.assertEqual(c.exception.stdout, "part")

    def test_error_frame_and_cut_stream(self):
        with self.assertRaises(Kosa8StreamError) as c:
            box_with(StreamOpener([frame("stderr", "e"), frame(done=True, error="host lost")])).exec_stream("x").result()
        self.assertEqual((str(c.exception), c.exception.stderr), ("host lost", "e"))
        with self.assertRaises(Kosa8StreamError):
            box_with(StreamOpener([frame("stdout", "a")])).exec_stream("x").result()

    def test_refusal_raises_typed_error_before_output(self):
        op = StreamOpener([], status=400, refusal={"error": "invalid_cmd", "message": "cmd must be a list"})
        with self.assertRaises(Kosa8BadRequestError):
            box_with(op).exec_stream(["x"])

    def test_timeout_bounds(self):
        box = box_with(StreamOpener([frame(done=True, exit=0)]))
        for bad in (0, 86401, 1.5, True):
            with self.assertRaises(ValueError):
                box.exec_stream("x", timeout_s=bad)

    def test_plain_opener_falls_back_to_whole_body(self):
        f = FakeCloud()
        sid = f.add_box()
        box = CloudSandbox(f.cloud(), f.boxes[sid])
        got = []
        r = box.exec(["echo", "hi"], on_stdout=got.append)
        self.assertEqual((got, r.exit_code), (["ran hi\n"], 0))
        self.assertTrue(f.last("POST /v1/sandboxes/")["body"]["stream"])
        box.exec(["echo", "hi"])
        self.assertNotIn("stream", f.last("POST /v1/sandboxes/")["body"])


class ShimStreamTest(unittest.TestCase):
    def setUp(self):
        self.f = FakeCloud()
        compat.set_default_cloud(self.f.cloud())
        e2b.CommandHandle.poll_interval = 0.01

    def tearDown(self):
        compat.set_default_cloud(None)
        e2b.CommandHandle.poll_interval = 1.0

    def exec_body(self):
        return self.f.last("POST /v1/sandboxes/")["body"]

    # e2b
    def test_e2b_run_with_callbacks_streams(self):
        sb = e2b.Sandbox.create()
        self.f.on_stream = lambda argv, env: [{"stream": "stdout", "data": "1\n"}, {"stream": "stderr", "data": "w\n"},
                                              {"stream": "stdout", "data": "2\n"}, {"done": True, "exit": 0}]
        out, err = [], []
        r = sb.commands.run("x", on_stdout=out.append, on_stderr=err.append)
        self.assertEqual((out, err, r.stdout, r.stderr), (["1\n", "2\n"], ["w\n"], "1\n2\n", "w\n"))
        self.assertEqual((self.exec_body()["stream"], self.exec_body()["timeout_s"]), (True, 60))

    def test_e2b_long_or_no_timeout_streams_short_stays_buffered(self):
        sb = e2b.Sandbox.create()
        sb.commands.run("sleep 100", timeout=150)
        self.assertEqual((self.exec_body()["stream"], self.exec_body()["timeout_s"]), (True, 150))
        sb.commands.run("serve", timeout=0)
        self.assertTrue(self.exec_body()["stream"])
        self.assertNotIn("timeout_s", self.exec_body())
        sb.commands.run("echo")
        self.assertNotIn("stream", self.exec_body())

    def test_e2b_stream_timeout_and_exit(self):
        sb = e2b.Sandbox.create()
        self.f.on_stream = lambda a, e: [{"done": True, "error": "ran past its timeout_s", "code": "exec_timeout"}]
        with self.assertRaises(e2b.TimeoutException):
            sb.commands.run("sleep 999", timeout=120)
        self.f.on_stream = lambda a, e: [{"stream": "stderr", "data": "bad"}, {"done": True, "exit": 2}]
        with self.assertRaises(e2b.CommandExitException) as c:
            sb.commands.run("false", on_stderr=lambda t: None)
        self.assertEqual((c.exception.exit_code, c.exception.stderr), (2, "bad"))

    def test_e2b_background_callbacks_get_each_new_piece(self):
        sb = e2b.Sandbox.create()
        self.f.bg_progress = ["a", "a", "ab", "abc"]
        got = []
        h = sb.commands.run("make", background=True, on_stdout=got.append)
        r = h.wait()
        self.assertEqual((got, r.stdout), (["a", "b", "c"], "abc"))
        self.f.bg_progress = ["x", "xy"]
        h2 = sb.commands.run("make", background=True)
        seen = []
        h2.wait(on_stdout=seen.append)
        self.assertEqual(seen, ["x", "y"])
        self.f.bg_progress = ["1", "12"]
        self.assertEqual(list(sb.commands.run("m", background=True)), [("1", None, None), ("2", None, None)])

    # daytona
    def test_daytona_long_timeout_streams(self):
        sb = daytona.Daytona(daytona.DaytonaConfig(api_key=KEY)).create()
        sb.process.exec("sleep 100", timeout=120)
        self.assertTrue(self.exec_body()["stream"])
        sb.process.exec("echo hi")
        self.assertNotIn("stream", self.exec_body())

    def test_daytona_sessions(self):
        sb = daytona.Daytona(daytona.DaytonaConfig(api_key=KEY)).create()
        p = sb.process
        p.create_session("s")
        with self.assertRaises(daytona.DaytonaConflictError):
            p.create_session("s")
        self.f.on_stream = lambda a, e: [{"stream": "stdout", "data": "1\n"}, {"stream": "stderr", "data": "e\n"},
                                         {"stream": "stdout", "data": "2\n"}, {"done": True, "exit": 0}]
        r = p.execute_session_command("s", daytona.SessionExecuteRequest(command="go"))
        self.assertEqual((r.exit_code, r.stdout, r.stderr, r.output), (0, "1\n2\n", "e\n", "1\ne\n2\n"))
        a = p.execute_session_command("s", daytona.SessionExecuteRequest(command="go", run_async=True))
        out, err = [], []
        asyncio.run(p.get_session_command_logs_async("s", a.cmd_id, out.append, err.append))
        self.assertEqual((out, err), (["1\n", "2\n"], ["e\n"]))
        self.assertEqual(p.get_session_command("s", a.cmd_id).exit_code, 0)
        self.assertEqual(p.get_session_command_logs("s", a.cmd_id).stdout, "1\n2\n")
        self.assertEqual([c.command for c in p.get_session("s").commands], ["go", "go"])
        self.assertEqual([s.session_id for s in p.list_sessions()], ["s"])
        p.delete_session("s")
        with self.assertRaises(daytona.DaytonaNotFoundError):
            p.get_session("s")

    # modal
    def test_modal_exec_iterates_lines_as_printed(self):
        app = modal.App.lookup("t", create_if_missing=True)
        sb = modal.Sandbox.create(app=app)
        self.f.on_stream = lambda a, e: [{"stream": "stdout", "data": "a\nb"}, {"stream": "stderr", "data": "x\n"},
                                         {"stream": "stdout", "data": "c\nd"}, {"done": True, "exit": 0}]
        p = sb.exec("sh", "-c", "work")
        self.assertTrue(self.exec_body()["stream"])
        self.assertEqual(list(p.stdout), ["a\n", "bc\n", "d"])
        self.assertEqual((p.wait(), p.poll(), p.returncode, p.stderr.read()), (0, 0, 0, "x\n"))

    def test_modal_entrypoint_stdout_follows_output(self):
        app = modal.App.lookup("t", create_if_missing=True)
        sb = modal.Sandbox.create("python", "-u", "train.py", app=app)
        self.f.bg_progress = ["step 1\n", "step 1\nstep 2\n", "step 1\nstep 2\ndone"]
        orig = compat.poll_output
        modal.poll_output = lambda status, interval=1.0: orig(status, 0.01)
        try:
            self.assertEqual(list(sb.stdout), ["step 1\n", "step 2\n", "done"])
        finally:
            modal.poll_output = orig


if __name__ == "__main__":
    unittest.main()
