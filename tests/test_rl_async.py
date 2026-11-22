"""AsyncCloud, async fork_map, AsyncRun and map_rollouts, against the fake API."""

import asyncio
import unittest
from unittest import mock

from fake_cloud import FakeCloud

from kosa8 import AsyncCloud, AsyncCloudSandbox, Kosa8ApiError, Kosa8ExecTimeoutError, Kosa8TimeoutError
from kosa8.rl import AsyncRun, map_rollouts

KEY = "k8c_live_testkey"
#: Tests never reach a real address: anything not stubbed fails here.
LOCAL = "http://127.0.0.1:9"


def run(coro):
    return asyncio.run(coro)


async def no_wait(_s):
    return None


class AsyncBase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=self.fake.aopen)
        p = mock.patch("kosa8.retry._asleep", no_wait)
        p.start()
        self.addCleanup(p.stop)


class AsyncCloudTest(AsyncBase):
    def test_sandbox_basics(self):
        async def go():
            box = await self.cloud.sandboxes.create(template="python", lease_s=300, run_id="run_a")
            self.assertIsInstance(box, AsyncCloudSandbox)
            self.assertEqual(box.info["labels"], {"kosa8.run": "run_a"})
            self.assertEqual((await box.exec(["echo", "hi"], env={"A": "1"})).stdout, "ok\n")
            self.assertEqual(self.fake.calls[-1][2], {"cmd": ["echo", "hi"], "env": ["A=1"]})
            await box.files.write("/workspace/a.txt", "hello")
            self.assertEqual(await box.files.read_text("/workspace/a.txt"), "hello")
            self.assertEqual([f["path"] for f in await box.files.list()], ["/workspace/a.txt"])
            kids = await box.fork(2, labels={"g": "0"}, lease_s=120)
            self.assertEqual(len(kids), 2)
            self.assertEqual(await self.cloud.sandboxes.heartbeat(run_id="run_a"), 1)
            g = await box.grade(["python", "/g.py"], files={"/g.py": "x"}, rollout={"step": 1})
            self.assertEqual(g.reward, 1.0)
            snap = await box.snapshot("s")
            self.assertTrue(snap["id"].startswith("snap_"))
            batch = await self.cloud.sandboxes.create_many(3, run_id="run_a")
            self.assertEqual(len(batch), 3)
            await (await self.cloud.sandboxes.get(box.id)).refresh()
            self.assertEqual(len(await self.cloud.sandboxes.list(labels={"kosa8.run": "run_a"})), 4)
            self.assertEqual((await self.cloud.sandboxes.drain(run_id="run_a"))["deleted"], 4)
            async with await self.cloud.sandboxes.create() as tmp:
                self.assertIn(tmp.id, self.fake.live())
            self.assertNotIn(tmp.id, self.fake.live())
            for k in kids:
                await k.delete()
            self.assertEqual(self.fake.live(), [])

        run(go())

    def test_errors_and_retries(self):
        async def go():
            n = {"i": 0}

            def flaky(m, p, b):
                if m == "GET":
                    n["i"] += 1
                    if n["i"] == 1:
                        return 503, {"error": "x", "message": "busy"}
                if p.endswith("/exec"):
                    return 504, {"error": "exec_timeout", "message": "ran past 90 s"}
                return None

            box = await self.cloud.sandboxes.create()
            self.fake.before = flaky
            await box.refresh()
            self.assertEqual(n["i"], 2)
            with self.assertRaises(Kosa8ExecTimeoutError):
                await box.exec("sleep 100")

            async def slow(*a):
                raise asyncio.TimeoutError()

            c2 = AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=slow)
            with self.assertRaises(Kosa8TimeoutError):
                await c2.me()

        run(go())

    def test_threaded_sync_opener_and_sync_twin(self):
        fake = FakeCloud()
        cloud = AsyncCloud(KEY, env={}, base_url=LOCAL, opener=fake)

        async def go():
            box = await cloud.sandboxes.create()
            self.assertIn(box.id, fake.live())
            await cloud.aclose()

        run(go())
        self.assertIs(cloud.sync()._open, fake)
        self.assertNotIn(KEY, repr(cloud))

    def test_create_cancelled_deletes(self):
        async def go():
            self.fake.create_state = "creating"
            self.fake.before = lambda m, p, b: (200, {"sandbox": {"id": p.rsplit("/", 1)[1], "state": "error"}}) \
                if m == "GET" and p.startswith("/v1/sandboxes/sbx_") else None
            with self.assertRaises(Exception):
                await self.cloud.sandboxes.create(labels={"a": "b"})
            self.assertEqual(self.fake.live(), [])

        run(go())


class AsyncForkMap(AsyncBase):
    def test_results_and_cleanup(self):
        async def go():
            base = await self.cloud.sandboxes.create()
            tries = {}

            async def fn(child, item):
                tries[item] = tries.get(item, 0) + 1
                if item == 2 and tries[item] == 1:
                    raise Kosa8ApiError("lost", status=502, code="host_failed")
                if item == 3:
                    await asyncio.sleep(5)
                return item * 10

            res = await self.cloud.fork_map(base, fn, [1, 2, 3], concurrency=2, timeout_s=0.1)
            self.assertEqual([r.value for r in res[:2]], [10, 20])
            self.assertEqual(res[1].attempts, 2)
            self.assertEqual(res[2].failure_class, "timeout")
            self.assertEqual(self.fake.live(), [base.id])

        run(go())

    def test_cancellation_deletes_children(self):
        async def go():
            base = await self.cloud.sandboxes.create()
            started = asyncio.Event()

            async def fn(child, item):
                started.set()
                await asyncio.sleep(30)

            task = asyncio.ensure_future(self.cloud.fork_map(base, fn, list(range(5)), concurrency=3))
            await started.wait()
            self.assertEqual(len(self.fake.live()), 4)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.fake.live(), [base.id])

        run(go())


class AsyncRuns(AsyncBase):
    def test_async_run_map(self):
        async def go():
            async with AsyncRun(self.cloud, "async-run", heartbeat_s=60) as r:
                base = await self.cloud.sandboxes.create(template="python")

                async def solve(sb, task):
                    await sb.files.write("/workspace/s.py", "x")

                tasks = [{"id": f"t{i}", "files": {"/grader/t.py": "x"}} for i in range(3)]
                res = await r.map(base, tasks, solve, grade_cmd=["python", "/grader/t.py"], group_size=2)
                self.assertEqual(len(res), 6)
                self.assertEqual(res.rewards, [1.0] * 6)
                res2 = await r.map(base, tasks[:1], lambda sb, t: asyncio.sleep(0, result=0.5))
                self.assertEqual((res2[0].reward, res2[0].step), (0.5, 1))
            self.assertEqual(self.fake.live(), [])
            self.assertEqual(self.fake.runs[r.id]["status"], "finished")
            self.assertEqual(len([x for x in self.fake.rollouts if x["from"] == "grade"]), 6)
            self.assertEqual(len([x for x in self.fake.rollouts if x["from"] == "sdk"]), 1)

        run(go())

    def test_masked_infra_and_no_run(self):
        async def go():
            base = await self.cloud.sandboxes.create()
            self.fake.before = lambda m, p, b: (503, {"error": "capacity", "message": "full"}) \
                if p.endswith("/fork") else None

            async def solve(sb, t):
                return 1.0

            res = await map_rollouts(solve, [{"id": "a"}], base=base, retries=1)
            self.assertEqual((res[0].failure_class, res[0].masked, res[0].reward, res[0].attempts),
                             ("infra", True, None, 2))
            self.fake.before = None

            async def make(task):
                return await self.cloud.sandboxes.create(image="img")

            res = await map_rollouts(solve, [{"id": "b"}], base=make, cloud=self.cloud)
            self.assertEqual(res[0].reward, 1.0)
            res = await map_rollouts(solve, [{"id": "c"}], base={"template": "python"}, cloud=self.cloud)
            self.assertTrue(res[0].ok)
            with self.assertRaises(ValueError):
                await map_rollouts(solve, [1], base={"template": "x"})
            self.assertEqual(self.fake.live(), [base.id])

        run(go())

    def test_crash_in_async_run_drains(self):
        async def go():
            with self.assertRaises(RuntimeError):
                async with AsyncRun(self.cloud, "boom") as r:
                    await self.cloud.sandboxes.create()
                    raise RuntimeError("x")
            self.assertEqual(self.fake.live(), [])
            self.assertEqual(self.fake.runs[r.id]["status"], "failed")

        run(go())


try:
    import httpx  # noqa: F401

    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class HttpxTransport(unittest.TestCase):
    def test_httpx_opener(self):
        import httpx

        from kosa8.aio import _HttpxOpener

        fake = FakeCloud()

        def handler(request):
            status, data, headers = fake(request.method, str(request.url), dict(request.headers),
                                         request.content or None, 10)
            return httpx.Response(status, content=data, headers=headers)

        async def go():
            cloud = AsyncCloud(KEY, env={}, base_url=LOCAL)
            self.assertIsInstance(cloud._aopen, _HttpxOpener)
            cloud._aopen._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            box = await cloud.sandboxes.create()
            self.assertEqual((await box.exec("true")).exit_code, 0)
            await box.files.write("/workspace/x", b"\x00\x01")
            self.assertEqual(await box.files.read("/workspace/x"), b"\x00\x01")

            def boom(request):
                raise httpx.ConnectError("refused")

            cloud._aopen._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
            cloud.retry = __import__("kosa8").NO_RETRY
            from kosa8 import Kosa8ConnectionError

            with self.assertRaises(Kosa8ConnectionError):
                await cloud.me()
            await cloud.aclose()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
