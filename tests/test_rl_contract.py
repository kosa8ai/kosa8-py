"""The SDK against the server contract of AzmxAI/kosa8 rl-integration
(fanout.js, forkSandbox, failures.js, idempotency.js, rl.js), as mirrored by
the fake: Idempotency-Key, answer shapes, limits, recording."""

import asyncio
import json
import re
import unittest
import uuid
from unittest import mock

import fake_cloud
from fake_cloud import FakeCloud

from kosa8 import (
    AsyncCloud,
    Kosa8Cloud,
    Kosa8Error,
    classify_failure,
    failure_of,
)
from kosa8.cloud import GRADE_MAX_BYTES, fit_meta, keyed_route
from kosa8.rl import Run, RolloutResult

KEY = "k8c_live_testkey"
#: Tests never reach a real address: anything not stubbed fails here.
LOCAL = "http://127.0.0.1:9"


def no_sleep(test):
    waits = []
    p = mock.patch("kosa8.retry._sleep", lambda s: waits.append(s))
    p.start()
    test.addCleanup(p.stop)
    return waits


def keys(fake, method, suffix):
    return [next((v for k, v in h.items() if k.lower() == "idempotency-key"), None)
            for m, p, h in fake.headers if m == method and p.endswith(suffix)]


class IdempotencyKeys(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=self.fake)
        self.waits = no_sleep(self)

    def test_sent_on_the_calls_that_make_sandboxes_only(self):
        box = self.cloud.sandboxes.create()
        self.cloud.sandboxes.create_many(2)
        box.fork(2)
        box.grade(["true"])
        box.exec("true")
        self.cloud.sandboxes.heartbeat(ids=[box])
        for suffix in ("/v1/sandboxes", "/batch", "/fork", "/grade"):
            (k,) = keys(self.fake, "POST", suffix)
            self.assertEqual(str(uuid.UUID(k)), k)  # a UUID
        self.assertEqual(keys(self.fake, "POST", "/exec"), [None])
        self.assertEqual(keys(self.fake, "POST", "/heartbeat"), [None])
        self.assertEqual(len(set(self.fake.idem)), 4)
        self.assertTrue(keyed_route("POST", "/v1/sandboxes/sbx_1/grade"))
        self.assertFalse(keyed_route("POST", "/v1/sandboxes/sbx_1/exec"))
        self.assertFalse(keyed_route("GET", "/v1/sandboxes"))

    def test_lost_answer_is_retried_with_the_same_key_and_replayed(self):
        lost = {"n": 0}

        def lose_first(m, p):
            if m == "POST" and p == "/v1/sandboxes" and lost["n"] == 0:
                lost["n"] += 1
                return True
            return False

        self.fake.lose_answer = lose_first
        box = self.cloud.sandboxes.create()
        k1, k2 = keys(self.fake, "POST", "/v1/sandboxes")
        self.assertEqual(k1, k2)
        self.assertEqual(self.fake.replays, 1)
        self.assertEqual(self.fake.live(), [box.id])  # one sandbox, not two

    def test_5xx_on_create_and_batch_is_retried(self):
        n = {"i": 0}

        def flaky(m, p, b):
            if m == "POST" and p in ("/v1/sandboxes", "/v1/sandboxes/batch"):
                n["i"] += 1
                if n["i"] % 2 == 1:
                    return 503, {"error": "host_unavailable", "message": "try again"}
            return None

        self.fake.before = flaky
        self.cloud.sandboxes.create()
        batch = self.cloud.sandboxes.create_many(3)
        self.assertEqual(len(batch), 3)
        for suffix in ("/v1/sandboxes", "/batch"):
            ks = keys(self.fake, "POST", suffix)
            # The SDK's own key is renewed after a 5xx: the retry is a new attempt.
            self.assertEqual((len(ks), len(set(ks))), (2, 2))
        self.assertEqual(len(self.fake.live()), 4)

    def test_grade_retried_after_a_lost_answer_records_once(self):
        with Run(self.cloud, "r") as run:
            box = self.cloud.sandboxes.create()
            gone = {"n": 0}

            def lose_once(m, p):
                if p.endswith("/grade") and gone["n"] == 0:
                    gone["n"] += 1
                    return True
                return False

            self.fake.lose_answer = lose_once
            g = box.grade(["true"], rollout={"step": 0})
            self.fake.lose_answer = None
            self.assertEqual((g.reward, g.recorded, g.run_id), (1.0, True, run.id))
        self.assertEqual(len([r for r in self.fake.rollouts if r["source"] == "grade"]), 1)
        self.assertEqual(self.fake.replays, 1)

    def test_in_progress_is_retried_after_retry_after_s(self):
        box = self.cloud.sandboxes.create()
        orig = self.fake._idempotent

        def first_then_busy(key, method, path, body, run):
            out = orig(key, method, path, body, run)
            if path.endswith("/fork") and key not in seen:
                seen.add(key)
                self.fake.in_progress.add(key)
                raise ConnectionError("dropped")  # the client retries; the server says still running
            return out

        seen = set()
        self.fake._idempotent = first_then_busy
        kids = box.fork(2)
        self.assertEqual(len(kids), 2)
        self.assertEqual(len(keys(self.fake, "POST", "/fork")), 3)  # first, 409, replay
        self.assertIn(2.0, self.waits)  # waited the 409's retry_after_s
        self.assertEqual(len(self.fake.live()), 3)

    def test_mismatch_is_raised_not_retried(self):
        self.cloud.sandboxes.create(idempotency_key="mine-1", template="a")
        with self.assertRaises(Kosa8Error) as cm:
            self.cloud.sandboxes.create(idempotency_key="mine-1", template="b")
        self.assertEqual(cm.exception.code, "idempotency_mismatch")
        self.assertEqual((cm.exception.status, classify_failure(cm.exception)), (422, ("client", False)))
        self.assertEqual(len(keys(self.fake, "POST", "/v1/sandboxes")), 2)
        # The same key with the same body is the first answer again: one sandbox.
        again = self.cloud.sandboxes.create(idempotency_key="mine-1", template="a")
        self.assertEqual(len(self.fake.live()), 1)
        self.assertEqual(again.id, self.fake.live()[0])

    def test_bad_key_and_fork_chunk_keys(self):
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create(idempotency_key="has space")
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create(idempotency_key="")
        self.assertEqual(self.fake.calls, [])
        box = self.cloud.sandboxes.create()
        box.fork(1500, idempotency_key="fk", partial_ok=True)
        self.assertEqual(keys(self.fake, "POST", "/fork"), ["fk", "fk-2"])

    def test_quota_refusal_is_not_kept(self):
        self.fake.running_limit = 0
        with self.assertRaises(Kosa8Error):
            self.cloud.sandboxes.create(idempotency_key="q1")
        self.assertNotIn("q1", self.fake.idem)  # a 429 is not kept: the same key works once there is room
        self.fake.running_limit = None
        self.cloud.sandboxes.create(idempotency_key="q1")
        self.assertEqual(len(self.fake.live()), 1)


class Shapes(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=self.fake)
        no_sleep(self)

    def test_batch_that_made_none_raises_the_first_refusal(self):
        self.fake.running_limit = 0
        with self.assertRaises(Kosa8Error) as cm:
            self.cloud.sandboxes.create_many(3, partial_ok=True)
        body = cm.exception.body
        self.assertEqual((cm.exception.status, body["requested"], body["created"]), (429, 3, 0))
        self.assertEqual(cm.exception.failure_class, "quota")
        self.fake.running_limit = 2
        with self.assertRaises(Kosa8Error) as cm:
            self.cloud.sandboxes.create_many(3, partial_ok=False)
        self.assertEqual(cm.exception.body["created"], 0)
        self.assertEqual(self.fake.live(), [])
        batch = self.cloud.sandboxes.create_many(3, run_id="run_x")
        self.assertEqual((len(batch), batch.requested, batch.created, batch.run_id), (2, 3, 2, "run_x"))
        self.assertEqual([f["index"] for f in batch.failed], [2])

    def test_fork_that_made_none_raises_partial_reports(self):
        box = self.cloud.sandboxes.create()
        self.fake.running_limit = 1
        with self.assertRaises(Kosa8Error) as cm:
            box.fork(3, run_id="run_f")
        self.assertEqual((cm.exception.body["requested"], cm.exception.body["created"]), (3, 0))
        self.assertEqual(len(cm.exception.body["failed"]), 3)
        with self.assertRaises(Kosa8Error) as cm:
            box.fork(2)  # the original answer, unchanged
        self.assertNotIn("requested", cm.exception.body)
        self.fake.running_limit = 3
        kids = box.fork(4, lease_s=120)
        self.assertEqual((len(kids), kids.requested, [f["index"] for f in kids.failed]), (2, 4, [2, 3]))
        self.assertEqual(kids.failed[0]["failure_class"], "quota")

    def test_drain_follows_more_and_heartbeat(self):
        for _ in range(5):
            self.cloud.sandboxes.create(lease_s=300, run_id="run_d")
        self.assertEqual(self.cloud.sandboxes.heartbeat(run_id="run_d"), 5)
        self.fake.drain_max = 2
        r = self.cloud.sandboxes.drain(run_id="run_d")
        self.assertEqual((r["deleted"], len(r["ids"]), len(self.fake.drains)), (5, 5, 3))
        self.assertEqual(self.fake.live(), [])

    def test_grade_answer_fields(self):
        box = self.cloud.sandboxes.create()
        self.fake.on_grade = lambda sb, b, f: {"exit": 0, "stdout": 'log\n{"reward": 0.25, "metrics": {"p": 1}}\n'}
        g = box.grade(["python", "t.py"], keep=True)
        self.assertEqual((g.reward, g.reward_from, g.metrics, g.failure_class, g.retryable, g.kept, g.sandbox_id),
                         (0.25, "json", {"p": 1}, "ok", False, True, box.id))
        self.assertIn(g.grader_id, self.fake.live())  # kept
        self.assertIsNone(g.recorded)
        # The command did not finish: no reward, infra, retryable.
        self.fake.on_grade = lambda sb, b, f: {"error": "host_failed", "message": "host lost", "status": 502}
        g = box.grade(["python", "t.py"])
        self.assertEqual((g.reward, g.exit_code, g.failure_class, g.retryable, g.error),
                         (None, None, "infra", True, "host_failed"))
        self.fake.on_grade = lambda sb, b, f: {"error": "exec_timeout", "message": "90 s", "status": 504}
        g = box.grade(["python", "t.py"])
        self.assertEqual((g.reward, g.failure_class, g.retryable), (None, "timeout", False))

    def test_grade_limits_checked_here(self):
        box = self.cloud.sandboxes.create()
        n0 = len(self.fake.calls)
        with self.assertRaisesRegex(ValueError, "at most 64 files"):
            box.grade(["t"], files={f"/g/{i}": "x" for i in range(65)})
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            box.grade(["t"], files={"/g/a": b"\0" * (GRADE_MAX_BYTES // 2), "/g/b": b"\0" * (GRADE_MAX_BYTES // 2 + 1)})
        for bad in (91, 0, 1.5, True):
            with self.assertRaises(ValueError):
                box.grade(["t"], timeout_s=bad)
        with self.assertRaises(ValueError):
            box.grade(["t"], files={"/g/../etc/x": "x"})
        self.assertEqual(len(self.fake.calls), n0)  # nothing was sent
        # The limits themselves pass (the old 64 KiB cap is gone).
        g = box.grade(["t"], files={f"/g/{i}": b"\0" * (GRADE_MAX_BYTES // 64) for i in range(64)}, timeout_s=90.0)
        self.assertEqual(g.reward, 1.0)
        self.assertEqual(self.fake.calls[-1][2]["timeout_s"], 90)

    def test_lease_bounds(self):
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create_many(2, lease_s=86401)
        with self.assertRaises(ValueError):
            self.cloud.sandboxes.create_many(2, lease_s=600, ttl_s=600)
        self.cloud.sandboxes.create_many(1, lease_s=86400)

    def test_failure_of_matches_the_server(self):
        cases = [(200, None), (400, "invalid_cmd"), (404, "not_found"), (409, "secrets"), (413, "too_large"),
                 (402, "payment_required"), (423, "org_frozen"), (429, "quota_exceeded"), (429, "rate_limited"),
                 (429, "x"), (400, "plan_feature"), (501, None), (501, "not_supported"), (409, "not_available"),
                 (502, "host_failed"), (503, None), (504, None), (504, "exec_timeout"), (500, "stream_timeout"),
                 (409, "starting"), (400, "too_soon"), (403, "too_many_runs"),
                 (400, "invalid_timeout"), (400, "bad_timeout_s"), (402, "spend_cap"), (402, "starter_limit"),
                 (402, "payment_failed"), (409, "idempotency_in_progress"), (503, "database_busy")]
        for status, code in cases:
            want = fake_cloud.failure_of(status, code)
            self.assertEqual(failure_of(status, code), (want["failure_class"], want["retryable"]), (status, code))


class Recording(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=self.fake)
        no_sleep(self)

    def test_graded_rollouts_recorded_once_by_the_server(self):
        with Run(self.cloud, "r") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, [{"id": "a"}, {"id": "b"}], lambda s, t: None, grade_cmd=["t"], group_size=2)
        self.assertTrue(all(r.recorded for r in res))
        self.assertEqual([r["source"] for r in self.fake.rollouts], ["grade"] * 4)
        self.assertEqual(sorted((r["group"], r["index"]) for r in self.fake.rollouts),
                         [("0", 0), ("0", 1), ("1", 0), ("1", 1)])

    def test_grade_that_did_not_finish_is_regraded_then_masked(self):
        calls = {"n": 0}

        def host_lost(sb, b, f):
            calls["n"] += 1
            return {"error": "host_failed", "message": "lost", "status": 502}

        self.fake.on_grade = host_lost
        solves = []
        with Run(self.cloud, "r") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, [{"id": "a"}], lambda s, t: solves.append(1), grade_cmd=["t"], retries=2)
        self.assertEqual(len(solves), 1)  # the agent ran once
        self.assertEqual(calls["n"], 3)  # graded three times, each with its own key (a replay would repeat it)
        self.assertEqual(len(set(keys(self.fake, "POST", "/grade"))), 3)
        r = res[0]
        self.assertEqual((r.reward, r.failure_class, r.masked, r.recorded), (None, "infra", True, True))
        self.assertIn("host_failed", r.error)
        # Each attempt is the server's own record (infra); the SDK adds none.
        self.assertEqual([x["source"] for x in self.fake.rollouts], ["grade"] * 3)

    def test_sdk_records_when_the_server_could_not(self):
        orig = self.fake._grade

        def unrecorded(sb, b):
            status, body = orig(sb, b)
            if body.get("recorded"):
                self.fake.rollouts.pop()
            return status, {**body, "recorded": False}

        self.fake._grade = unrecorded
        with Run(self.cloud, "r") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, [{"id": "a"}], lambda s, t: None, grade_cmd=["t"])
        self.assertFalse(res[0].recorded)
        self.assertEqual([x["source"] for x in self.fake.rollouts], ["api"])

    def test_rows_fit_the_server(self):
        r = RolloutResult(task=None, group=3, index=0, failure_class="error", error="ValueError: x",
                          stdout="y" * 10000, meta={"big": "z" * 20000, "task": "t1"})
        row = r.to_row()
        self.assertEqual((row["failure_class"], row["group"], len(row["stdout_tail"])), ("client", "3", 4096))
        self.assertLessEqual(len(json.dumps(row["meta"], separators=(",", ":"))), 8192)
        self.assertEqual((row["meta"]["task"], row["meta"]["sdk_failure_class"], row["meta"]["truncated"]),
                         ("t1", "error", True))
        with Run(self.cloud, "r") as run:
            run.record([r, r.to_row()], flush=True)
        self.assertEqual(len([x for x in self.fake.rollouts if x["source"] == "api"]), 2)

    def test_fit_meta(self):
        self.assertEqual(fit_meta({"a": 1}), {"a": 1})
        m = fit_meta({"a": "x" * 9000, "b": 2, "c": {"d": "y" * 100}})
        self.assertLessEqual(len(json.dumps(m, separators=(",", ":"))), 8192)
        self.assertEqual((m["b"], m["c"], m["truncated"]), (2, {"d": "y" * 100}, True))
        self.assertTrue(m["a"].startswith("xxx"))
        self.assertEqual(fit_meta({"o": object()}).keys(), {"o"})


class FlushKeysPrefixSwebench(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=self.fake)
        self.waits = no_sleep(self)

    def test_flush_key_is_kept_with_its_rows_until_answered(self):
        with Run(self.cloud, "f") as run:
            path = f"/v1/rl/runs/{run.id}/rollouts"
            # Every answer of the first flush is lost (the server stored the rows on the first try).
            self.fake.lose_answer = lambda m, p: m == "POST" and p == path
            run.record([{"step": 0, "reward": 1.0}] * 3, flush=False)
            with self.assertRaises(Kosa8Error):
                run.flush()
            self.assertEqual(run.pending, 3)
            self.fake.lose_answer = None
            run.record({"step": 1}, flush=False)
            self.assertEqual(run.flush(), 4)  # the 3 again (a replay), then the new one
            ks = keys(self.fake, "POST", "/rollouts")
            self.assertEqual(len(ks), 5)  # 3 tries, the resend, the new batch
            self.assertEqual(len(set(ks[:4])), 1)
            self.assertNotEqual(ks[4], ks[0])
        self.assertEqual(len([r for r in self.fake.rollouts if r["source"] == "api"]), 4)  # stored once
        self.assertEqual(self.fake.replays, 3)

    def test_flush_retried_on_5xx(self):
        n = {"i": 0}

        def once(m, p, b):
            if p.endswith("/rollouts") and m == "POST" and n["i"] == 0:
                n["i"] += 1
                return 502, {"error": "internal", "message": "x"}
            return None

        with Run(self.cloud, "f") as run:
            self.fake.before = once
            run.record([{"step": 0}] * 2, flush=False)
            self.assertEqual(run.flush(), 2)
            self.fake.before = None
        self.assertEqual(len(keys(self.fake, "POST", "/rollouts")), 2)

    def test_create_sends_labels_in_the_body(self):
        self.fake.create_state = "creating"
        seen = {}
        orig = self.fake._route

        def route(method, path, query, body):
            if method == "GET" and path.startswith("/v1/sandboxes/sbx_") and "during" not in seen:
                # While it boots, a drain by run id already sees it.
                seen["during"] = [s["id"] for s in self.fake._match({"run_id": "run_l"})]
            return orig(method, path, query, body)

        self.fake._route = route
        box = self.cloud.sandboxes.create(run_id="run_l", labels={"team": "a"})
        create = [b for m, p, b in self.fake.calls if m == "POST" and p == "/v1/sandboxes"][-1]
        self.assertEqual(create["labels"], {"team": "a", "kosa8.run": "run_l"})
        self.assertEqual(seen["during"], [box.id])
        self.assertFalse([c for c in self.fake.calls if c[1].endswith("/labels")])  # no second call
        self.fake.create_state = "running"
        self.cloud.sandboxes.create()
        self.assertNotIn("labels", [b for m, p, b in self.fake.calls if m == "POST" and p == "/v1/sandboxes"][-1])

    def test_prefix(self):
        batch = self.cloud.sandboxes.create_many(2, prefix="w")
        self.assertEqual([self.fake.sandboxes[b.id]["name"] for b in batch], ["w-0001", "w-0002"])
        self.assertEqual([b for m, p, b in self.fake.calls if p.endswith("/batch")][-1]["prefix"], "w")
        for bad in ("-x", "a b", "x" * 60):
            with self.assertRaises(ValueError):
                self.cloud.sandboxes.create_many(1, prefix=bad)

    def test_swebench_grade_too_long(self):
        from kosa8.rl import swebench
        from kosa8.rl.swebench import GradeTooLong

        inst = {"instance_id": "a__b-1", "test_patch": "diff", "FAIL_TO_PASS": ["t::x"], "PASS_TO_PASS": []}
        box = self.cloud.sandboxes.create()
        n0 = len(self.fake.calls)
        with self.assertRaisesRegex(GradeTooLong, "split"):
            swebench.grade(box, inst, timeout_s=600)
        self.assertEqual(len(self.fake.calls), n0)
        self.fake.on_grade = lambda sb, b, f: {"error": "exec_timeout", "message": "90 s", "status": 504}
        with self.assertRaises(GradeTooLong) as cm:
            swebench.grade(box, inst)
        self.assertIn("Jobs API", str(cm.exception))
        self.assertEqual(classify_failure(cm.exception), ("timeout", False))


class AsyncContract(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=self.fake.aopen)

        async def no_wait(_s):
            return None

        p = mock.patch("kosa8.retry._asleep", no_wait)
        p.start()
        self.addCleanup(p.stop)

    def test_keys_retries_and_shapes(self):
        async def go():
            lost = set()

            def lose_first(m, p):
                route = "grade" if p.endswith("/grade") else p
                if m == "POST" and re.search(r"/v1/sandboxes$|/grade$", p) and route not in lost:
                    lost.add(route)
                    return True
                return False

            self.fake.lose_answer = lose_first
            box = await self.cloud.sandboxes.create()
            g = await box.grade(["t"], files={"/g/t.py": "x"})
            self.fake.lose_answer = None
            self.assertEqual((g.reward, g.reward_from), (1.0, "json"))
            for suffix in ("/v1/sandboxes", "/grade"):
                ks = keys(self.fake, "POST", suffix)
                self.assertEqual((len(ks), len(set(ks))), (2, 1))
            self.assertEqual(self.fake.replays, 2)
            self.fake.running_limit = 1
            with self.assertRaises(Kosa8Error) as cm:
                await box.fork(2, run_id="run_a")
            self.assertEqual(cm.exception.body["created"], 0)
            self.fake.running_limit = None
            self.fake.drain_max = 1
            await box.fork(2, run_id="run_a")
            r = await self.cloud.sandboxes.drain(run_id="run_a")
            self.assertEqual(r["deleted"], 2)
            with self.assertRaises(ValueError):
                await box.grade(["t"], files={f"/g/{i}": "" for i in range(65)})

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
