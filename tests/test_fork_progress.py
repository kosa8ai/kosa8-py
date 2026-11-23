"""CloudSandbox.fork(on_progress=...): the fork's progress streamed as NDJSON
(Accept: application/x-ndjson), then the same answer as a plain fork."""

import json
import unittest

from kosa8 import CloudSandbox, Kosa8ApiError, Kosa8Cloud

KEY = "k8c_live_testkey"


def ndjson(*events):
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


class Opener:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        self.seen.append({"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None})
        return self.answer(self.seen[-1])


def box(opener):
    return CloudSandbox(Kosa8Cloud(KEY, env={}, base_url="http://localhost:8787", opener=opener),
                        {"id": "sbx_1", "state": "running", "template": "base"})


class ForkProgressTest(unittest.TestCase):
    def test_events_then_the_answer(self):
        kids = [{"id": f"sbx_k{i}", "state": "running", "source": "fork", "parent_id": "sbx_1"} for i in range(150)]
        op = Opener(lambda s: (200, ndjson(
            {"event": "progress", "requested": 150, "created": 0, "failed": 0},
            {"event": "progress", "requested": 150, "created": 100, "failed": 0},
            {"event": "progress", "requested": 150, "created": 150, "failed": 0},
            {"event": "result", "status": 201, "parent": "sbx_1", "sandboxes": kids, "requested": 150, "created": 150, "failed": []},
        ), {"Content-Type": "application/x-ndjson"}))
        seen = []
        out = box(op).fork(150, on_progress=seen.append)
        self.assertEqual(len(out), 150)
        self.assertEqual([p["created"] for p in seen], [0, 100, 150])
        self.assertEqual(seen[0], {"requested": 150, "created": 0, "failed": 0})
        h = op.seen[0]["headers"]
        self.assertIn("application/x-ndjson", h["Accept"])
        self.assertTrue(h.get("Idempotency-Key"))
        self.assertEqual(op.seen[0]["body"], {"n": 150})

    def test_a_failure_in_the_stream_raises_the_usual_error(self):
        op = Opener(lambda s: (200, ndjson(
            {"event": "progress", "requested": 2, "created": 0, "failed": 0},
            {"event": "result", "status": 409, "error": "fork_refused", "message": "the host refused the fork"},
        )))
        with self.assertRaises(Kosa8ApiError) as e:
            box(op).fork(2, on_progress=lambda p: None)
        self.assertEqual(e.exception.status, 409)

    def test_an_api_that_does_not_stream_answers_plain_json(self):
        op = Opener(lambda s: (201, json.dumps({"parent": "sbx_1", "sandboxes": [{"id": "sbx_k", "state": "running"}]}).encode()))
        seen = []
        out = box(op).fork(1, on_progress=seen.append)
        self.assertEqual([k.id for k in out], ["sbx_k"])
        self.assertEqual(seen, [])

    def test_without_on_progress_nothing_changes(self):
        op = Opener(lambda s: (201, json.dumps({"parent": "sbx_1", "sandboxes": [{"id": "sbx_k", "state": "running"}]}).encode()))
        box(op).fork(1)
        self.assertEqual(op.seen[0]["headers"]["Accept"], "application/json")


if __name__ == "__main__":
    unittest.main()
