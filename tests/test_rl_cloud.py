"""Batch, fork options, leases, drain, grade, failure classes, retries and
fork_map on the sync client, against the in-memory fake API."""

import base64
import random
import threading
import time
import unittest
from unittest import mock

from fake_cloud import FakeCloud

from kosa8 import (
    NO_RETRY,
    Kosa8ApiError,
    Kosa8Cloud,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8ExecTimeoutError,
    Kosa8RateLimitError,
    Kosa8TimeoutError,
    RetryPolicy,
    SandboxBatch,
    classify_failure,
)

KEY = "k8c_live_testkey"
#: Tests never reach a real address: anything not stubbed fails here.
LOCAL = "http://127.0.0.1:9"


def no_sleep(test):
    waits = []
    p = mock.patch("kosa8.retry._sleep", lambda s: waits.append(s))
    p.start()
    test.addCleanup(p.stop)
    return waits


def cloud_for(fake, **kw):
    return Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=fake, **kw)


def bodies(fake, method, suffix):
    return [b for m, p, b in fake.calls if m == method and p.endswith(suffix)]


class FailureClasses(unittest.TestCase):
    def err(self, status, code=None, body=None, cls=Kosa8ApiError):
        return cls("x", status=status, code=code, body=body)

    def test_classes(self):
        cases = [
            (self.err(503), ("infra", True)),
            (self.err(502, "host_failed"), ("infra", True)),
            (self.err(409, "not_ready"), ("infra", True)),
            (self.err(429, "rate_limited"), ("quota", True)),
            (self.err(429, "quota_exceeded"), ("quota", False)),
            (self.err(402, "payment_required"), ("quota", False)),
            (self.err(423, "org_frozen"), ("quota", False)),
            (self.err(400, "invalid_cmd"), ("client", False)),
            (self.err(404), ("client", False)),
            (self.err(501, "not_supported"), ("client", False)),
            (Kosa8ExecTimeoutError("t", status=504, code="exec_timeout"), ("timeout", False)),
            (Kosa8TimeoutError("t"), ("infra", True)),
            (Kosa8ConnectionError("c"), ("infra", True)),
            (ValueError("bug"), ("error", False)),
            (ConnectionResetError("reset"), ("infra", True)),
        ]
        for e, want in cases:
            self.assertEqual(classify_failure(e), want, repr(e))

    def test_server_class_wins(self):
        e = self.err(500, body={"error": "x", "failure_class": "quota", "retryable": False})
        self.assertEqual((e.failure_class, e.retryable), ("quota", False))
        e = self.err(400, body={"failure_class": "infra"})
        self.assertEqual((e.failure_class, e.retryable), ("infra", True))

    def test_error_bodies_carry_class(self):
        fake = FakeCloud()
        fake.before = lambda m, p, b: (503, {"error": "capacity", "message": "full", "failure_class": "infra",
                                             "retryable": True})
        no_sleep(self)
        with self.assertRaises(Kosa8ApiError) as e:
            cloud_for(fake).sandboxes.create()
        self.assertEqual((e.exception.failure_class, e.exception.retryable), ("infra", True))

    def test_exec_failure_class(self):
        fake = FakeCloud()
        c = cloud_for(fake)
        box = c.sandboxes.create()
        self.assertEqual(box.exec("true").failure_class, "ok")
        fake.on_exec = lambda sb, a, e: {"exit": 3, "stdout": "", "stderr": "no"}
        self.assertEqual(box.exec("false").failure_class, "exit_nonzero")
        fake.on_exec = lambda sb, a, e: {"exit": 1, "stdout": "", "stderr": "", "failure_class": "timeout"}
        self.assertEqual(box.exec("x").failure_class, "timeout")


class Retries(unittest.TestCase):
    def test_policy_delay(self):
        p = RetryPolicy(attempts=5, base_s=1, max_s=4)
        rng = random.Random(1)
        for attempt, cap in ((1, 1), (2, 2), (3, 4), (4, 4)):
            for _ in range(20):
                self.assertLessEqual(p.delay(None, attempt, rng=rng), cap)
        e = Kosa8RateLimitError("slow", status=429)
        e.retry_after = 7.0
        self.assertGreaterEqual(p.delay(e, 1), 7.0)
        e.retry_after = 600
        self.assertEqual(p.delay(e, 1), 60.0)  # capped
        with self.assertRaises(ValueError):
            RetryPolicy(attempts=0)

    def test_get_retried_on_infra_then_ok(self):
        fake = FakeCloud()
        c = cloud_for(fake)
        box = c.sandboxes.create()
        n = {"i": 0}

        def flaky(m, p, b):
            if m == "GET":
                n["i"] += 1
                if n["i"] < 3:
                    return 503, {"error": "unavailable", "message": "busy"}
            return None

        fake.before = flaky
        waits = no_sleep(self)
        self.assertEqual(box.refresh().state, "running")
        self.assertEqual(n["i"], 3)
        self.assertEqual(len(waits), 2)

    def test_post_not_retried_on_5xx_but_on_429(self):
        fake = FakeCloud()
        fake.before = lambda m, p, b: (502, {"error": "host_failed", "message": "boom"})
        waits = no_sleep(self)
        with self.assertRaises(Kosa8ApiError):
            cloud_for(fake).sandboxes.create()
        self.assertEqual(len(fake.calls), 1)  # a create might have happened: never sent twice
        fake2 = FakeCloud()
        n = {"i": 0}

        def limited(m, p, b):
            n["i"] += 1
            if n["i"] == 1:
                return 429, {"error": "rate_limited", "message": "slow"}, {"Retry-After": "3"}
            return None

        fake2.before = limited
        box = cloud_for(fake2).sandboxes.create()
        self.assertTrue(box.id.startswith("sbx_"))
        self.assertGreaterEqual(waits[-1], 3.0)

    def test_heartbeat_and_drain_are_retried(self):
        fake = FakeCloud()
        n = {"i": 0}

        def once(m, p, b):
            if p.endswith("/heartbeat") and n["i"] == 0:
                n["i"] += 1
                return 500, {"error": "internal", "message": "x"}
            return None

        fake.before = once
        no_sleep(self)
        self.assertEqual(cloud_for(fake).sandboxes.heartbeat(run_id="run_1"), 0)
        self.assertEqual(len(bodies(fake, "POST", "/heartbeat")), 2)

    def test_no_retry_and_quota(self):
        fake = FakeCloud()
        fake.before = lambda m, p, b: (503, {"error": "x", "message": "y"})
        with self.assertRaises(Kosa8ApiError):
            cloud_for(fake, retry=NO_RETRY).sandboxes.list()
        self.assertEqual(len(fake.calls), 1)
        fake = FakeCloud()
        fake.before = lambda m, p, b: (402, {"error": "payment_required", "message": "pay"})
        with self.assertRaises(Kosa8Error):
            cloud_for(fake).sandboxes.list()
        self.assertEqual(len(fake.calls), 1)

    def test_connection_errors_retried(self):
        calls = []

        def boom(*a):
            calls.append(a)
            raise OSError("refused")

        no_sleep(self)
        with self.assertRaises(Kosa8ConnectionError):
            Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=boom).me()
        self.assertEqual(len(calls), 3)


class BatchForkLeaseDrainGrade(unittest.TestCase):
    def setUp(self):
        no_sleep(self)
        self.fake = FakeCloud()
        self.cloud = cloud_for(self.fake)

    def test_create_lease_and_run_label(self):
        box = self.cloud.sandboxes.create(template="python", lease_s=600, run_id="run_9", labels={"team": "rl"})
        body = bodies(self.fake, "POST", "/v1/sandboxes")[0]
        self.assertEqual(body, {"template": "python", "lease_s": 600})
        self.assertEqual(box.info["labels"], {"team": "rl", "kosa8.run": "run_9"})
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create(lease_s=10)
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create(labels={str(i): "x" for i in range(17)})

    def test_create_deletes_when_it_cannot_finish(self):
        def bad_labels(m, p, b):
            return (400, {"error": "invalid_labels", "message": "no"}) if p.endswith("/labels") else None

        self.fake.before = bad_labels
        with self.assertRaises(Kosa8ApiError):
            self.cloud.sandboxes.create(labels={"a": "b"})
        self.assertEqual(self.fake.live(), [])

    def test_create_many(self):
        batch = self.cloud.sandboxes.create_many(3, template="python", run_id="run_1", lease_s=300,
                                                 labels={"k": "v"}, cpus=2, mem_mb=4096)
        self.assertIsInstance(batch, SandboxBatch)
        self.assertEqual((len(batch), batch.created, batch.requested, batch.failed), (3, 3, 3, []))
        body = bodies(self.fake, "POST", "/batch")[0]
        self.assertEqual(body, {"count": 3, "partial_ok": True, "template": "python", "cpus": 2, "mem_mb": 4096,
                                "lease_s": 300, "run_id": "run_1", "labels": {"k": "v"}})
        self.assertEqual(self.fake.sandboxes[batch[0].id]["labels"], {"k": "v", "kosa8.run": "run_1"})
        for bad in (0, 1001, True):
            with self.assertRaises(ValueError):
                self.cloud.sandboxes.create_many(bad)
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create_many(2, cpus=2)

    def test_create_many_partial_and_all_or_nothing(self):
        self.fake.running_limit = 2
        batch = self.cloud.sandboxes.create_many(4)
        self.assertEqual(len(batch), 2)
        self.assertEqual([f["code"] for f in batch.failed], ["running_limit", "running_limit"])
        self.assertEqual(batch.failed[0]["failure_class"], "quota")
        self.fake.sandboxes.clear()
        with self.assertRaises(Kosa8Error):
            self.cloud.sandboxes.create_many(4, partial_ok=False)
        self.assertEqual(self.fake.live(), [])

    def test_create_many_waits_and_drops_failed_boots(self):
        self.fake.create_state = "creating"
        orig = self.fake._new

        def new(**info):
            sb = orig(**info)
            if sb["id"] == "sbx_2":
                sb["next_state"] = "error"
            return sb

        self.fake._new = new
        with mock.patch("time.sleep", lambda s: None):
            batch = self.cloud.sandboxes.create_many(3)
        self.assertEqual([b.state for b in batch], ["running", "running"])
        self.assertEqual(batch.failed[0]["id"], "sbx_2")
        self.assertNotIn("sbx_2", self.fake.live())

    def test_fork_options_and_chunks(self):
        base = self.cloud.sandboxes.create()
        kids = base.fork(3, labels={"g": "1"}, run_id="run_2", lease_s=120, partial_ok=True)
        self.assertEqual(len(kids), 3)
        self.assertEqual(bodies(self.fake, "POST", "/fork")[-1],
                         {"n": 3, "labels": {"g": "1"}, "run_id": "run_2", "lease_s": 120, "partial_ok": True})
        self.assertEqual(self.fake.sandboxes[kids[0].id]["labels"], {"g": "1", "kosa8.run": "run_2"})
        self.fake.calls.clear()
        kids = base.fork(150)
        self.assertEqual(len(kids), 150)
        self.assertEqual([b["n"] for b in bodies(self.fake, "POST", "/fork")], [100, 50])
        self.assertEqual(bodies(self.fake, "POST", "/fork")[0], {"n": 100})  # what older servers take

    def test_fork_failure_deletes_earlier_chunks(self):
        base = self.cloud.sandboxes.create()
        n = {"i": 0}

        def second_fails(m, p, b):
            if p.endswith("/fork"):
                n["i"] += 1
                if n["i"] == 2:
                    return 402, {"error": "spending_cap_reached", "message": "cap"}
            return None

        self.fake.before = second_fails
        with self.assertRaises(Kosa8Error):
            base.fork(120)
        self.assertEqual(self.fake.live(), [base.id])

    def test_heartbeat_and_drain(self):
        a = self.cloud.sandboxes.create(lease_s=600, run_id="run_3")
        self.cloud.sandboxes.create(run_id="run_3")
        other = self.cloud.sandboxes.create()
        self.assertEqual(self.cloud.sandboxes.heartbeat(run_id="run_3", extend_s=900), 1)
        self.assertEqual(self.fake.heartbeats[-1], {"run_id": "run_3", "extend_s": 900})
        self.assertEqual(a.heartbeat(), 1)
        self.assertEqual(self.fake.heartbeats[-1], {"ids": [a.id]})
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.heartbeat()
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.drain()
        r = self.cloud.sandboxes.drain(run_id="run_3")
        self.assertEqual(r, {"deleted": 2, "failed": []})
        self.assertEqual(self.fake.live(), [other.id])
        self.assertEqual(self.cloud.sandboxes.drain(labels={"x": "y"}, ids=[other])["deleted"], 0)
        self.assertEqual(self.fake.drains[-1], {"labels": {"x": "y"}, "ids": [other.id]})

    def test_grade(self):
        box = self.cloud.sandboxes.create()
        seen = {}

        def grade(sb, body, files):
            seen.update(body=body, files=files)
            return {"reward": 0.5, "metrics": {"passed": 2}, "exit": 0, "stdout": "x\n{\"reward\": 0.5}",
                    "stderr": "", "failure_class": "ok", "ms": 40, "grader_id": "sbx_g"}

        self.fake.on_grade = grade
        g = box.grade(["python", "/grader/test.py"], files={"/grader/test.py": "print(1)", "/grader/b.bin": b"\x00"},
                      timeout_s=60, env={"A": "1"}, run_id="run_4", rollout={"step": 2, "group": 0, "index": 1})
        self.assertEqual((g.reward, g.metrics, g.exit_code, g.failure_class, g.ms, g.grader_id),
                         (0.5, {"passed": 2}, 0, "ok", 40, "sbx_g"))
        self.assertEqual(seen["files"], {"/grader/test.py": b"print(1)", "/grader/b.bin": b"\x00"})
        b = seen["body"]
        self.assertEqual(b["files"]["/grader/test.py"], base64.b64encode(b"print(1)").decode())
        self.assertEqual((b["cmd"], b["timeout_s"], b["env"], b["run_id"], b["rollout"]),
                         (["python", "/grader/test.py"], 60, ["A=1"], "run_4", {"step": 2, "group": 0, "index": 1}))
        self.assertNotIn("keep", b)
        # Defaults from the exit code when the server sends no reward.
        self.fake.on_grade = lambda sb, body, files: {"exit": 1, "stdout": "", "stderr": "boom"}
        g = self.cloud.sandboxes.grade(box.id, "pytest -q", keep=True)
        self.assertEqual((g.reward, g.failure_class), (0.0, "exit_nonzero"))
        self.assertEqual(self.fake.calls[-1][2]["cmd"], ["sh", "-c", "pytest -q"])
        self.assertTrue(self.fake.calls[-1][2]["keep"])
        with self.assertRaises(ValueError):
            box.grade(["x"], files={"relative/path": "x"})
        with self.assertRaises(ValueError):
            box.grade(["x"], timeout_s=0)

    def test_context_manager_deletes(self):
        with self.assertRaises(RuntimeError):
            with self.cloud.sandboxes.create() as box:
                self.assertIn(box.id, self.fake.live())
                raise RuntimeError("agent crashed")
        self.assertEqual(self.fake.live(), [])


class ForkMap(unittest.TestCase):
    def setUp(self):
        no_sleep(self)
        self.fake = FakeCloud()
        self.cloud = cloud_for(self.fake)
        self.base = self.cloud.sandboxes.create()

    def test_results_in_order_and_children_deleted(self):
        def fn(child, item):
            self.assertNotEqual(child.id, self.base.id)
            return item * 2

        res = self.cloud.fork_map(self.base, fn, [1, 2, 3, 4, 5], concurrency=3)
        self.assertEqual([r.value for r in res], [2, 4, 6, 8, 10])
        self.assertTrue(all(r.ok and r.attempts == 1 for r in res))
        self.assertEqual(self.fake.live(), [self.base.id])
        forks = bodies(self.fake, "POST", "/fork")
        self.assertEqual(forks[0], {"n": 3, "partial_ok": True})  # preforked in one call

    def test_infra_retried_on_fresh_child_others_not(self):
        tries = {}

        def fn(child, item):
            tries.setdefault(item, []).append(child.id)
            if item == "flaky" and len(tries[item]) == 1:
                raise Kosa8ApiError("host lost", status=502, code="host_failed")
            if item == "bad":
                raise Kosa8ApiError("bad", status=400, code="invalid_cmd")
            if item == "bug":
                raise KeyError("oops")
            return "ok"

        res = self.cloud.fork_map(self.base, fn, ["flaky", "bad", "bug"], concurrency=2, retries=2)
        self.assertEqual([(r.failure_class, r.attempts) for r in res], [("ok", 2), ("client", 1), ("error", 1)])
        self.assertNotEqual(tries["flaky"][0], tries["flaky"][1])
        self.assertIsInstance(res[2].error, KeyError)
        self.assertEqual(self.fake.live(), [self.base.id])

    def test_retries_exhausted(self):
        def fn(child, item):
            raise Kosa8ConnectionError("down")

        res = self.cloud.fork_map(self.base, fn, [1], retries=1)
        self.assertEqual((res[0].failure_class, res[0].attempts), ("infra", 2))
        self.assertEqual(self.fake.live(), [self.base.id])

    def test_timeout_deletes_child(self):
        release = threading.Event()

        def fn(child, item):
            release.wait(5)
            return "late"

        res = self.cloud.fork_map(self.base, fn, [1], timeout_s=0.05)
        release.set()
        self.assertEqual(res[0].failure_class, "timeout")
        self.assertEqual(self.fake.live(), [self.base.id])

    def test_keyboard_interrupt_deletes_every_child(self):
        started = threading.Barrier(3, timeout=5)
        gate = threading.Event()

        def fn(child, item):
            if item == 0:
                started.wait()
                raise KeyboardInterrupt
            started.wait()
            gate.wait(5)  # still running when the map is interrupted
            return item

        with self.assertRaises(KeyboardInterrupt):
            self.cloud.fork_map(self.base, fn, [0, 1, 2, 3, 4], concurrency=3)
        self.assertEqual(self.fake.live(), [self.base.id])
        gate.set()
        time.sleep(0.05)
        self.assertEqual(self.fake.live(), [self.base.id])  # nothing made after the interrupt either

    def test_fork_errors_surface_per_item(self):
        self.fake.before = lambda m, p, b: (402, {"error": "payment_required", "message": "pay"}) \
            if p.endswith("/fork") else None
        res = self.cloud.fork_map(self.base, lambda c, i: i, [1, 2])
        self.assertEqual([r.failure_class for r in res], ["quota", "quota"])
        with self.assertRaises(ValueError):
            self.cloud.fork_map(self.base, lambda c, i: i, [1], concurrency=0)


if __name__ == "__main__":
    unittest.main()
