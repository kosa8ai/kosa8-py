# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Streams never report complete early: a background command's output is read
again from the byte a lost connection stopped at, and a local run stream's
final frame is only a result when it says ok."""
import unittest
from unittest import mock

from kosa8 import Kosa8Error
from kosa8.aio import AsyncCloudSandbox
from kosa8.cloud import CloudSandbox

from test_cloud_api import both, run

FULL = "one\ntwo\nthree\n"


def output_api():
    """GET …/exec/bg_1?offset=N: the command writes a line per read, one read
    fails with a 502, and it exits after the fourth (exit 3)."""
    reads = {"n": 0}

    def answer(s):
        reads["n"] += 1
        n = reads["n"]
        if n == 2:
            return 502, {"error": "host_failed", "message": "tunnel"}
        off = int(s["query"]["offset"][0])
        written = {1: 4, 3: 8}.get(n, len(FULL))
        end = min(written, off + 5)
        done = n >= 4
        return 200, {"exec": {"id": "bg_1", "state": "exited" if done else "running", "exit": 3 if done else None,
                              "output": FULL[off:end], "next_offset": end, "output_more": end < written}}
    return answer


class Follow(unittest.TestCase):
    def test_follow_reads_everything_once_and_retries_from_the_same_byte(self):
        sync, _, stub = both(output_api())
        from kosa8.cloud_features import CloudBackgroundExec
        job = CloudBackgroundExec(sync, "/v1/sandboxes/sbx_1", {"id": "bg_1", "state": "running"})
        with mock.patch("time.sleep", lambda _s: None):
            got = "".join(job.follow())
        self.assertEqual(got, FULL)
        self.assertEqual(job.info["exit"], 3)
        offsets = [s["query"]["offset"][0] for s in stub.seen]
        self.assertEqual(offsets[:3], ["0", "4", "4"], "a failed read is tried again from the same byte")
        self.assertTrue(all(s["path"] == "/v1/sandboxes/sbx_1/exec/bg_1" for s in stub.seen))

    def test_follow_async(self):
        _, aio, _ = both(output_api())
        from kosa8.cloud_features import AsyncCloudBackgroundExec
        job = AsyncCloudBackgroundExec(aio, "/v1/sandboxes/sbx_1", {"id": "bg_1", "state": "running"})

        async def go():
            parts = []
            async for t in job.follow(interval=0):
                parts.append(t)
            return "".join(parts)

        self.assertEqual(run(go()), FULL)
        self.assertEqual(job.info["exit"], 3)

    def test_read_checks_offset_and_a_refusal_is_final(self):
        sync, _, stub = both(lambda s: (404, {"error": "not_found", "message": "no such background command"}))
        from kosa8.cloud_features import CloudBackgroundExec
        job = CloudBackgroundExec(sync, "/v1/sandboxes/sbx_1", {"id": "bg_1", "state": "running"})
        with self.assertRaises(ValueError):
            job.read(-1)
        with self.assertRaises(Kosa8Error):
            list(job.follow())
        self.assertEqual(len(stub.seen), 1, "a 404 is not retried")


class LocalRunEnd(unittest.TestCase):
    def test_a_final_frame_without_ok_is_not_exit_0(self):
        from kosa8.client import _collect
        from kosa8.errors import Kosa8StreamError
        with self.assertRaises(Kosa8StreamError):
            _collect(iter([{"stream": "stdout", "data": b"x"}, {"done": True}]), None, None, None)
        self.assertEqual(_collect(iter([{"done": True, "ok": True}]), None, None, None).exit_code, 0)


if __name__ == "__main__":
    unittest.main()
