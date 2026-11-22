"""cloud.jobs (hosted jobs) and kosa8.rl.Run.compare / Run.rollouts(as_=...),
against the in-memory fake API."""

import asyncio
import base64
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fake_cloud import FakeCloud

import kosa8
from kosa8 import Kosa8ApiError, Kosa8Cloud
from kosa8.errors import Kosa8TimeoutError
from kosa8.jobs import JOB_DONE_STATES, job_body, load_tasks
from kosa8.rl import Run

KEY = "k8c_live_testkey"
LOCAL = "http://127.0.0.1:9"


def setup_cloud(test):
    p = mock.patch("kosa8.retry._sleep", lambda s: None)
    p.start()
    test.addCleanup(p.stop)
    fake = FakeCloud()
    return fake, Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=fake)


def tasks(n):
    return [{"id": f"t{i}", "input": {"n": i}} for i in range(n)]


SOLVER = {"cmd": "python /workspace/solve.py", "timeout_s": 600}


def b64(s):
    return base64.b64encode(s.encode()).decode()


class Submit(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)

    def body(self):
        return [b for m, p, b in self.fake.calls if m == "POST" and p == "/v1/jobs"][-1]

    def test_submit_body_and_answer(self):
        job = self.cloud.jobs.submit(
            name="eval", template="python", tasks=tasks(3), solver=SOLVER,
            grader={"cmd": ["pytest", "-q", "/hidden/test.py"], "files": {"/hidden/test.py": "assert 1"}, "timeout_s": 60},
            concurrency=8, budget_usd=5, project="swe")
        self.assertTrue(job["id"].startswith("job_"))
        self.assertTrue(job["run_id"].startswith("run_"))
        self.assertEqual(job["status"], "queued")
        b = self.body()
        self.assertEqual(b["name"], "eval")
        self.assertEqual(b["template"], "python")
        self.assertEqual(b["tasks"], [{"id": "t0", "input": {"n": 0}}, {"id": "t1", "input": {"n": 1}}, {"id": "t2", "input": {"n": 2}}])
        self.assertEqual(b["solver"], SOLVER)
        self.assertEqual(b["grader"], {"cmd": ["pytest", "-q", "/hidden/test.py"], "timeout_s": 60,
                                       "files": {"/hidden/test.py": b64("assert 1")}})
        self.assertEqual((b["concurrency"], b["budget_usd"], b["project"]), (8, 5, "swe"))
        self.assertNotIn("retries_on_infra", b)

    def test_submit_carries_an_idempotency_key_and_replays(self):
        kw = dict(name="eval", image="python:3.12", tasks=tasks(2), solver=SOLVER, grader="pytest -q")
        a = self.cloud.jobs.submit(idempotency_key="job-1", **kw)
        b = self.cloud.jobs.submit(idempotency_key="job-1", **kw)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.fake.jobs), 1)
        self.assertEqual(self.fake.replays, 1)
        self.cloud.jobs.submit(**kw)
        hdrs = [h for m, p, h in self.fake.headers if p == "/v1/jobs" and m == "POST"]
        self.assertTrue(all("Idempotency-Key" in h for h in hdrs), "a key is sent even when none is given")
        self.assertEqual(len(self.fake.jobs), 2)

    def test_submit_retries_a_dropped_answer_once(self):
        kw = dict(name="eval", image="python:3.12", tasks=tasks(2), solver=SOLVER, grader="pytest -q")
        dropped = []

        def lose(method, path):
            if path == "/v1/jobs" and method == "POST" and not dropped:
                dropped.append(1)
                return True
            return False

        self.fake.lose_answer = lose
        job = self.cloud.jobs.submit(**kw)
        self.assertEqual(len(self.fake.jobs), 1, "the retry was the same request")
        self.assertIn(job["id"], self.fake.jobs)

    def test_grader_files_from_bytes_str_and_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "test.py"
            p.write_bytes(b"from disk")
            self.cloud.jobs.submit(name="e", snapshot="snap_1", tasks=tasks(1), solver="true", solver_timeout_s=30,
                                   grader="pytest", grader_files={"/h/a.py": p, "/h/b.bin": b"\x00\x01", "/h/c.txt": "text"},
                                   grader_timeout_s=20)
        g = self.body()["grader"]
        self.assertEqual(g["files"], {"/h/a.py": b64("from disk"), "/h/b.bin": "AAE=", "/h/c.txt": b64("text")})
        self.assertEqual(g["timeout_s"], 20)
        self.assertEqual(self.body()["solver"], {"cmd": "true", "timeout_s": 30})

    def test_tasks_from_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tasks.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"id": "a", "input": {"q": 1}}) + "\n\n")
                f.write(json.dumps({"id": 7, "files": {"/w/x": b64("x")}}) + "\n")
            self.assertEqual(load_tasks(path), [{"id": "a", "input": {"q": 1}}, {"id": "7", "files": {"/w/x": b64("x")}}])
            self.cloud.jobs.submit(name="e", template="python", tasks=path, solver=SOLVER, grader="true")
        self.assertEqual(len(self.body()["tasks"]), 2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.jsonl")
            with open(path, "w") as f:
                f.write("{nope\n")
            with self.assertRaisesRegex(ValueError, "bad.jsonl:1"):
                load_tasks(path)

    def test_checks_before_sending(self):
        ok = dict(name="e", template="python", tasks=tasks(1), solver=SOLVER, grader="true")
        bad = [
            dict(ok, template=None),  # no source
            dict(ok, image="x"),  # two sources
            dict(ok, tasks=[]),
            dict(ok, tasks=[{"id": "a"}, {"id": "a"}]),
            dict(ok, tasks=[{"input": 1}]),
            dict(ok, solver={"cmd": "x"}),  # no timeout
            dict(ok, grader={"cmd": "x", "files": {"rel/path": "x"}}),
            dict(ok, grader={"cmd": "x", "nope": 1}),
            dict(ok, solver={"cmd": "x", "timeout_s": 5, "files": {"/a": "b"}}),
        ]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                job_body(**kw)
        with self.assertRaises(ValueError):
            job_body(**dict(ok, tasks=[{"id": str(i)} for i in range(10001)]))
        self.assertEqual(self.fake.calls, [])

    def test_api_refusal_is_raised(self):
        self.fake.before = lambda m, p, b: (400, {"error": "invalid_concurrency", "message": "concurrency must be 1 to 20"}) if p == "/v1/jobs" else None
        with self.assertRaises(Kosa8ApiError) as cm:
            self.cloud.jobs.submit(name="e", template="python", tasks=tasks(1), solver=SOLVER, grader="true", concurrency=99)
        self.assertEqual(cm.exception.code, "invalid_concurrency")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)
        self.job = self.cloud.jobs.submit(name="e", template="python", tasks=tasks(3), solver=SOLVER, grader="true")

    def test_get_list_cancel(self):
        j = self.cloud.jobs.get(self.job["id"])
        self.assertEqual((j["status"], j["progress"]["done"], j["counts"]["done"]), ("running", 1, 1))
        other = self.cloud.jobs.submit(name="f", template="python", tasks=tasks(1), solver=SOLVER, grader="true", project="p")
        self.assertEqual([x["id"] for x in self.cloud.jobs.list()], [other["id"], self.job["id"]])
        self.assertEqual([x["id"] for x in self.cloud.jobs.list(project="p")], [other["id"]])
        self.assertEqual(len(self.cloud.jobs.list(limit=1)), 1)
        self.assertIn(("GET", "/v1/jobs", None), self.fake.calls)
        c = self.cloud.jobs.cancel(self.job["id"])
        self.assertEqual((c["status"], c["counts"]["cancelled"]), ("cancelled", 2))
        self.assertIn("cancelled", JOB_DONE_STATES)
        with self.assertRaises(Kosa8ApiError):
            self.cloud.jobs.get("job_nope")

    def test_wait_calls_progress_until_done(self):
        seen = []
        with mock.patch("kosa8.jobs.time.sleep", lambda s: None):
            final = self.cloud.jobs.wait(self.job["id"], progress=lambda j: seen.append(j["progress"]["done"]), poll_s=0.01)
        self.assertEqual(final["status"], "done")
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(self.fake.runs[final["run_id"]]["status"], "finished")

    def test_wait_timeout(self):
        self.fake.job_step = 0
        with self.assertRaises(Kosa8TimeoutError):
            self.cloud.jobs.wait(self.job["id"], poll_s=0.01, timeout=0.001)


class Estimate(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)
        self.kw = dict(name="e", template="python", tasks=tasks(10), solver=SOLVER, grader={"cmd": "true", "timeout_s": 60})

    def test_estimate_ranges(self):
        e = self.cloud.jobs.estimate(**self.kw)
        self.assertEqual(e["basis"], "timeout")
        self.assertLessEqual(e["sandbox_hours"]["low"], e["sandbox_hours"]["high"])
        self.assertIn("included_hours_left", e)
        self.assertEqual(self.fake.jobs, {}, "an estimate starts nothing")

    def test_sample_starts_a_pilot_then_sample_job_id(self):
        e = self.cloud.jobs.estimate(sample=2, **self.kw)
        pilot = e["sample"]["job_id"]
        self.assertEqual(self.fake.jobs[pilot]["total"], 2)
        e2 = self.cloud.jobs.estimate(sample_job_id=pilot, **self.kw)
        self.assertEqual(e2["basis"], "sample")
        body = [b for m, p, b in self.fake.calls if p == "/v1/jobs/estimate"][-1]
        self.assertEqual(body["sample_job_id"], pilot)
        with self.assertRaises(ValueError):
            self.cloud.jobs.estimate(sample=2, sample_job_id=pilot, **self.kw)


class AsyncJobs(unittest.TestCase):
    def test_async_twin(self):
        p = mock.patch("kosa8.retry._sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        fake = FakeCloud()

        async def main():
            async with kosa8.AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=fake.aopen) as cloud:
                job = await cloud.jobs.submit(name="e", template="python", tasks=tasks(2), solver=SOLVER, grader="true")
                seen = []

                async def prog(j):
                    seen.append(j["status"])

                with mock.patch("kosa8.jobs.asyncio.sleep", mock.AsyncMock()):
                    final = await cloud.jobs.wait(job["id"], progress=prog, poll_s=0)
                listed = await cloud.jobs.list()
                est = await cloud.jobs.estimate(name="e", template="python", tasks=tasks(2), solver=SOLVER, grader="true")
                other = await cloud.jobs.submit(name="f", template="python", tasks=tasks(2), solver=SOLVER, grader="true")
                cancelled = await cloud.jobs.cancel(other["id"])
                got = await cloud.jobs.get(job["id"])
                return final, seen, listed, est, cancelled, got

        final, seen, listed, est, cancelled, got = asyncio.run(main())
        self.assertEqual(final["status"], "done")
        self.assertEqual(seen, ["running", "done"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(est["basis"], "timeout")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(got["status"], "done")


class RunCompareAndRollouts(unittest.TestCase):
    def setUp(self):
        self.fake, self.cloud = setup_cloud(self)

    def _run(self, name, n):
        run = Run(self.cloud, name, signals=False)
        run.start()
        self.addCleanup(run.finish)
        run.record([{"step": 0, "group": f"g{i}", "reward": i / 10, "metrics": {"passed": i}, "meta": {"t": i}}
                    for i in range(n)], flush=True)
        return run

    def test_compare_rows(self):
        a, b = self._run("a", 3), self._run("b", 1)
        rich = {"run": {"id": "run_x", "name": "x", "status": "finished", "project": "p", "created_at": 1},
                "summary": {"rollouts": 10, "ok": 9, "success_rate": 0.9, "infra_rate": 0.1, "steps": 2,
                            "reward": {"mean": 0.5, "p50": 0.4, "min": 0, "max": 1},
                            "by_failure_class": {"ok": 9, "infra": 1},
                            "usage": {"sandboxes": 10, "sandbox_hours": 1.5, "cost_usd": 0.2}},
                "recommendations": [{"id": "mask_infra"}]}
        rows = Run.compare([a, b.id, rich], cloud=self.cloud)
        self.assertEqual([r["name"] for r in rows], ["a", "b", "x"])
        self.assertEqual([r["rollouts"] for r in rows[:2]], [3, 1])
        x = rows[2]
        self.assertEqual((x["reward_mean"], x["reward_p50"], x["sandbox_hours"], x["cost_usd"], x["fc_ok"], x["fc_infra"],
                          x["recommendations"], x["success_rate"]), (0.5, 0.4, 1.5, 0.2, 9, 1, 1, 0.9))
        self.assertEqual(set(rows[0]), set(x), "every row has the same columns")
        with self.assertRaises(ValueError):
            Run.compare(["run_1"])
        with self.assertRaises(TypeError):
            Run.compare([42], cloud=self.cloud)

    def test_rollouts_records_pages_all(self):
        run = self._run("a", 3)
        with mock.patch("kosa8.rl._run.MAX_ROWS_PER_CALL", 2):
            rows = run.rollouts(as_="records")
        self.assertEqual([r["group"] for r in rows], ["g0", "g1", "g2"])
        pages = [p for m, p, b in self.fake.calls if m == "GET" and p.endswith("/rollouts")]
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(run.rollouts(as_="records", limit=2)), 2)
        # Without as_, one page as before.
        self.assertIn("rollouts", run.rollouts())
        with self.assertRaises(ValueError):
            run.rollouts(as_="csv")

    def test_pandas_and_arrow_are_optional(self):
        run = self._run("a", 2)
        for as_, mod in (("pandas", "pandas"), ("arrow", "pyarrow")):
            with mock.patch.dict("sys.modules", {mod: None}):
                n = len(self.fake.calls)
                with self.assertRaisesRegex(ImportError, f"pip install {mod}"):
                    run.rollouts(as_=as_)
                self.assertEqual(len(self.fake.calls), n, "no request before the import fails")

    def test_pandas_frame(self):
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas is not installed")
        df = self._run("a", 2).rollouts(as_="pandas")
        self.assertEqual(len(df), 2)
        self.assertIn("metrics.passed", df.columns)

    def test_arrow_table(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow is not installed")
        t = self._run("a", 2).rollouts(as_="arrow")
        self.assertEqual(t.num_rows, 2)
        self.assertEqual(json.loads(t.column("metrics")[1].as_py()), {"passed": 1})


if __name__ == "__main__":
    unittest.main()
