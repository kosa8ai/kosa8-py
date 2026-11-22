"""kosa8.rl: Run (record, finish, map), Lease (heartbeats, drain on exit,
crash, SIGTERM and interpreter exit), against the in-memory fake API."""

import json
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest import mock

from fake_cloud import FakeCloud

import kosa8
from kosa8 import Kosa8ApiError, Kosa8Cloud
from kosa8.rl import DEFAULT_MASK, Lease, RolloutResult, Rollouts, Run, lease

KEY = "k8c_live_testkey"
#: Tests never reach a real address: anything not stubbed fails here.
LOCAL = "http://127.0.0.1:9"
GRADER = "print('hidden')"


def setup_cloud(test):
    p = mock.patch("kosa8.retry._sleep", lambda s: None)
    p.start()
    test.addCleanup(p.stop)
    fake = FakeCloud()
    return fake, Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=fake)


def tasks(n):
    return [{"id": f"t{i}", "prompt": "add", "files": {"/grader/test.py": GRADER}} for i in range(n)]


class RunLifecycle(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)

    def test_quickstart_shape(self):
        """The README / dashboard quickstart, end to end."""
        cloud = self.cloud
        seen = []

        def solve(sandbox, task):
            seen.append(sandbox.id)
            sandbox.files.write("/workspace/solution.py", "def add(a, b): return a + b")

        with Run(cloud, "my-grpo-run") as run:
            base = cloud.sandboxes.create(template="python")
            results = run.map(base, tasks(4), solve, grade_cmd=["python", "/grader/test.py"], concurrency=64)

        self.assertIsInstance(results, Rollouts)
        self.assertEqual([r.reward for r in results], [1.0] * 4)
        self.assertEqual([r.failure_class for r in results], ["ok"] * 4)
        self.assertFalse(any(r.masked for r in results))
        self.assertTrue(all(r.recorded for r in results))
        # The base was labelled and leased by the open run; the run was created, then finished.
        rid = run.id
        create = [b for m, p, b in self.fake.calls if m == "POST" and p == "/v1/sandboxes"][0]
        self.assertEqual(create, {"template": "python", "lease_s": 600, "labels": {"kosa8.run": run.id}})
        self.assertEqual(self.fake.runs[rid]["status"], "finished")
        self.assertEqual(self.fake.runs[rid]["name"], "my-grpo-run")
        # Children are forks of base, graded with the hidden files and run id, then deleted; at exit
        # the run's sandboxes (the base) were drained.
        grades = [b for m, p, b in self.fake.calls if p.endswith("/grade")]
        self.assertEqual(len(grades), 4)
        self.assertEqual(grades[0]["files"], {"/grader/test.py": "cHJpbnQoJ2hpZGRlbicp"})
        self.assertEqual(grades[0]["run_id"], rid)
        self.assertEqual(sorted(g["rollout"]["group"] for g in grades), ["0", "1", "2", "3"])  # the grader takes a string
        self.assertEqual({g["rollout"]["step"] for g in grades}, {0})
        self.assertEqual(grades[0]["rollout"]["meta"]["task"][0], "t")
        self.assertNotIn(base.id, seen)
        self.assertEqual(self.fake.live(), [])
        self.assertEqual(self.fake.drains[-1], {"run_id": rid})
        # Graded rollouts are recorded by the grader, not again by the SDK.
        self.assertEqual([r["from"] for r in self.fake.rollouts], ["grade"] * 4)
        self.assertIsNone(getattr(cloud, "_run_defaults", None))

    def test_crash_drains_and_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            with Run(self.cloud, "crashy") as run:
                self.cloud.sandboxes.create_many(3)
                self.cloud.sandboxes.create()
                raise RuntimeError("trainer died")
        self.assertEqual(self.fake.live(), [])
        self.assertEqual(self.fake.runs[run.id]["status"], "failed")
        batch = [b for m, p, b in self.fake.calls if p.endswith("/batch")][0]
        self.assertEqual((batch["run_id"], batch["lease_s"]), (run.id, 600))

    def test_keyboard_interrupt_in_map_cleans_up(self):
        gate = threading.Event()

        def solve(sandbox, task):
            if task["id"] == "t0":
                raise KeyboardInterrupt
            gate.wait(5)
            return 1.0

        with self.assertRaises(KeyboardInterrupt):
            with Run(self.cloud, "ctrl-c") as run:
                base = self.cloud.sandboxes.create()
                run.map(base, tasks(6), solve, concurrency=3)
        gate.set()
        time.sleep(0.05)
        self.assertEqual(self.fake.live(), [])
        self.assertEqual(self.fake.runs[run.id]["status"], "failed")

    def test_attach_and_drain_off(self):
        run = Run.attach(self.cloud, "run_existing", drain_on_exit=False)
        self.fake.runs["run_existing"] = {"id": "run_existing", "status": "running"}
        with run:
            keep = self.cloud.sandboxes.create()
        self.assertEqual(self.fake.live(), [keep.id])
        self.assertEqual(self.fake.drains, [])
        self.assertFalse([c for c in self.fake.calls if c[1] == "/v1/rl/runs" and c[0] == "POST"])
        with self.assertRaises(ValueError):
            Run(self.cloud)

    def test_record_batches_and_reading(self):
        with Run(self.cloud, "rec", flush_every=3, project="p1", config={"lr": 1e-6}) as run:
            run.record({"step": 0, "reward": 1.0})
            run.record([{"step": 0, "reward": 0.0}, {"step": 1, "reward": 0.5}])  # reaches 3: sent
            self.assertEqual(run.pending, 0)
            run.record(RolloutResult(task="x", group=0, index=0, step=1, reward=0.25, failure_class="ok"))
            self.assertEqual(run.pending, 1)
            self.assertEqual(run.summary()["summary"]["rollouts"], 3)
            page = run.rollouts(step=1, failure_class="ok", min_reward=0.1, limit=10)
            self.assertEqual(page["query"], {"step": "1", "failure_class": "ok", "min_reward": "0.1", "limit": "10"})
        self.assertEqual(run.recorded, 4)  # the last one went at finish
        created = self.fake.runs[run.id]
        self.assertEqual((created["project"], created["config"]), ("p1", {"lr": 1e-6}))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.jsonl")
            data = run.export(path)
            self.assertEqual(len(data.splitlines()), 4)
            with open(path, "rb") as f:
                self.assertEqual(json.loads(f.readline())["reward"], 1.0)
        self.assertEqual([r["id"] for r in Run.list(self.cloud, project="p1")], [run.id])

    def test_record_failure_keeps_rows(self):
        with Run(self.cloud, "rec2") as run:
            run.record([{"step": 0}] * 2, flush=False)
            self.fake.before = lambda m, p, b: (503, {"error": "x", "message": "y"}) if p.endswith("/rollouts") else None
            with self.assertRaises(Kosa8ApiError):
                run.flush()
            self.assertEqual(run.pending, 2)
            self.fake.before = None
            self.assertEqual(run.flush(), 2)
        rows = [{"step": i} for i in range(1200)]
        with Run(self.cloud, "big", flush_every=500) as run:
            run.record(rows, flush=False)
            self.assertEqual(run.flush(), 1200)
        sizes = [len(b["rollouts"]) for m, p, b in self.fake.calls if p.endswith(f"{run.id}/rollouts")]
        self.assertEqual(sizes, [500, 500, 200])


class RunMap(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)

    def test_infra_failures_masked_not_zero(self):
        self.fake.before = lambda m, p, b: (503, {"error": "capacity", "message": "no room",
                                                 "failure_class": "infra", "retryable": True}) \
            if p.endswith("/fork") else None
        with Run(self.cloud, "masked") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, tasks(2), lambda s, t: None, grade_cmd=["true"], retries=2)
        for r in res:
            self.assertEqual((r.failure_class, r.masked, r.reward, r.attempts), ("infra", True, None, 3))
        self.assertEqual(res.rewards, [None, None])
        self.assertEqual(res.mask, [False, False])
        sdk_rows = [r for r in self.fake.rollouts if r["from"] == "sdk"]
        self.assertEqual(len(sdk_rows), 2)
        self.assertTrue(sdk_rows[0]["meta"]["masked"])
        self.assertEqual(sdk_rows[0]["failure_class"], "infra")
        self.assertNotIn("reward", sdk_rows[0])
        self.assertEqual(DEFAULT_MASK, ("infra", "quota", "error"))

    def test_grade_retried_alone(self):
        solves = []
        n = {"i": 0}

        def flaky_grade(sb, body, files):
            n["i"] += 1
            if n["i"] == 1:
                return (502, {"error": "host_failed", "message": "grader host lost"})
            return {"reward": 0.0, "exit": 1, "stdout": "", "stderr": "", "failure_class": "exit_nonzero"}

        self.fake.on_grade = flaky_grade
        with Run(self.cloud, "g") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, tasks(1), lambda s, t: solves.append(t), grade_cmd=["pytest"])
        self.assertEqual(len(solves), 1)  # the agent ran once; only the grader was retried
        self.assertEqual((res[0].reward, res[0].failure_class, res[0].masked, res[0].ok), (0.0, "exit_nonzero", False, True))

    def test_reward_from_solve_groups_steps(self):
        def solve(sandbox, task):
            return {"reward": 0.5 if task["id"] == "t0" else 1.0, "tokens": 120, "meta": {"m": 1}}

        with Run(self.cloud, "r") as run:
            base = self.cloud.sandboxes.create()
            first = run.map(base, tasks(2), solve, group_size=3, concurrency=4)
            second = run.map(base, tasks(1), solve)
        self.assertEqual(len(first), 6)
        self.assertEqual([[r.reward for r in g] for g in first.groups()], [[0.5] * 3, [1.0] * 3])
        self.assertEqual([(r.group, r.index) for r in first], [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])
        self.assertEqual({r.step for r in first} | {r.step for r in second}, {0, 1})
        self.assertAlmostEqual(first.mean_reward, 0.75)
        rows = [r for r in self.fake.rollouts if r["from"] == "sdk"]
        self.assertEqual(len(rows), 7)
        self.assertEqual((rows[0]["tokens"], rows[0]["meta"]["m"]), (120, 1))

    def test_reward_fn_errors_and_timeouts(self):
        def solve(sandbox, task):
            if task["id"] == "t1":
                raise ZeroDivisionError("harness bug")
            if task["id"] == "t2":
                time.sleep(1)
            return "done"

        with Run(self.cloud, "e") as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, tasks(3), solve, reward=lambda s, t, out: 1.0 if out == "done" else 0.0,
                          timeout_s=0.2, mask=("infra", "error", "timeout"))
            nores = run.map(base, tasks(1), lambda s, t: "no reward here")
        self.assertEqual((res[0].reward, res[0].failure_class), (1.0, "ok"))
        self.assertEqual((res[1].failure_class, res[1].masked), ("error", True))
        self.assertIn("ZeroDivisionError", res[1].error)
        self.assertEqual((res[2].failure_class, res[2].masked, res[2].reward), ("timeout", True, None))
        self.assertEqual(nores[0].failure_class, "error")
        self.assertIn("no reward", nores[0].error)
        self.assertEqual(self.fake.live(), [])

    def test_base_variants(self):
        made = []

        def make(task):
            b = self.cloud.sandboxes.create(image="img:" + task["id"])
            made.append(b.id)
            return b

        with Run(self.cloud, "v") as run:
            res = run.map(make, tasks(2), lambda s, t: 1.0)
            res2 = run.map({"template": "python"}, tasks(1), lambda s, t: 1.0)
            res3 = run.map("snap_abc", tasks(1), lambda s, t: 1.0)
            with self.assertRaises(ValueError):
                run.map("not-a-snapshot", tasks(1), lambda s, t: 1.0)
        self.assertEqual(len(made), 2)
        self.assertTrue(all(r.ok for r in list(res) + list(res2) + list(res3)))
        creates = [b for m, p, b in self.fake.calls if m == "POST" and p == "/v1/sandboxes"]
        creates = [{k: v for k, v in c.items() if k != "labels"} for c in creates]  # each also has kosa8.run
        self.assertIn({"template": "python", "lease_s": 600}, creates)
        self.assertIn({"snapshot": "snap_abc", "lease_s": 600}, creates)
        self.assertEqual(self.fake.live(), [])

    def test_keep_and_validation(self):
        with Run(self.cloud, "k", drain_on_exit=False) as run:
            base = self.cloud.sandboxes.create()
            res = run.map(base, tasks(1), lambda s, t: 1.0, keep=True)
            self.assertIn(res[0].sandbox_id, self.fake.live())
            for bad in ({"group_size": 0}, {"retries": -1}, {"mask": ("nope",)}, {"concurrency": 0}):
                with self.assertRaises(ValueError):
                    run.map(base, tasks(1), lambda s, t: 1.0, **bad)
        with self.assertRaises(ValueError):
            Run(self.cloud, "closed").map(base, tasks(1), lambda s, t: 1.0)


class Leases(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)

    def test_heartbeats_then_drain(self):
        a = self.cloud.sandboxes.create(lease_s=600, labels={"job": "j1"})
        with lease(self.cloud, labels={"job": "j1"}, lease_s=120, interval_s=0.01) as ls:
            deadline = time.monotonic() + 5
            while ls.beats < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertGreaterEqual(ls.beats, 2)
        self.assertEqual(self.fake.heartbeats[0], {"labels": {"job": "j1"}, "extend_s": 120})
        self.assertEqual((ls.drained["deleted"], ls.drained["failed"]), (1, []))
        self.assertNotIn(a.id, self.fake.live())
        self.assertTrue(ls.closed)
        self.assertEqual(ls.close(), ls.drained)  # idempotent

    def test_heartbeat_errors_do_not_stop_it(self):
        self.fake.before = lambda m, p, b: (400, {"error": "x", "message": "no"}) if p.endswith("/heartbeat") else None
        with self.assertLogs("kosa8.rl", "WARNING"):
            ls = Lease(self.cloud, run_id="run_x", interval_s=0.01).start()
            deadline = time.monotonic() + 5
            while ls.last_error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsInstance(ls.last_error, Kosa8ApiError)
            self.assertTrue(ls._thread.is_alive())
            ls.close()
        self.assertFalse(ls._thread.is_alive())

    def test_drain_at_interpreter_exit(self):
        registered = []
        with mock.patch("kosa8.rl._lease.atexit.register", registered.append), \
                mock.patch("kosa8.rl._lease.atexit.unregister", lambda f: None):
            b = self.cloud.sandboxes.create(run_id="run_e", lease_s=600)
            Lease(self.cloud, run_id="run_e", interval_s=60).start()
            self.assertEqual(len(registered), 1)
            registered[0]()  # what atexit runs if nobody closed it
        self.assertNotIn(b.id, self.fake.live())

    def test_sigterm_drains_then_exits(self):
        b = self.cloud.sandboxes.create(run_id="run_t", lease_s=600)
        prev = signal.getsignal(signal.SIGTERM)
        ls = Lease(self.cloud, run_id="run_t", interval_s=60).start()
        self.assertEqual(signal.getsignal(signal.SIGTERM), ls._on_term)
        with self.assertRaises(SystemExit) as e:
            ls._on_term(signal.SIGTERM, None)
        self.assertEqual(e.exception.code, 128 + signal.SIGTERM)
        self.assertNotIn(b.id, self.fake.live())
        self.assertEqual(signal.getsignal(signal.SIGTERM), prev)

    def test_drain_failure_is_logged_not_raised(self):
        self.fake.before = lambda m, p, b: (500, {"error": "x", "message": "y"}) if p.endswith("/drain") else None
        ls = Lease(self.cloud, run_id="r", interval_s=60, signals=False).start()
        with self.assertLogs("kosa8.rl", "WARNING"):
            self.assertIsNone(ls.close())

    def test_validation(self):
        with self.assertRaises(ValueError):
            Lease(self.cloud)
        with self.assertRaises(ValueError):
            Lease(self.cloud, run_id="r", lease_s=5)
        self.assertEqual(Lease(self.cloud, run_id="r", lease_s=600).interval_s, 200.0)


class TopLevel(unittest.TestCase):
    def test_cloud_alias(self):
        self.assertIs(kosa8.Cloud, kosa8.Kosa8Cloud)
        self.assertEqual(kosa8.__version__, "0.5.0")


if __name__ == "__main__":
    unittest.main()
