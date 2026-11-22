"""The framework adapters: Gymnasium, TRL, OpenEnv, verifiers, NeMo Gym and
SWE-bench. Frameworks that are not installed are stood in for by small
modules with the same names and shapes (taken from their source), so these
tests check the adapter's side of each contract."""

import asyncio
import dataclasses
import inspect
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fake_cloud import FakeCloud

from kosa8 import AsyncCloud, Kosa8Cloud

KEY = "k8c_live_testkey"
LOCAL = "http://127.0.0.1:9"


def sync_cloud(test):
    p = mock.patch("kosa8.retry._sleep", lambda s: None)
    p.start()
    test.addCleanup(p.stop)
    fake = FakeCloud()
    return fake, Kosa8Cloud(KEY, env={}, base_url=LOCAL, opener=fake)


class _Model:
    """Enough of a pydantic model: annotated defaults, keyword init, model_dump."""

    def __init__(self, **kw):
        for klass in reversed(type(self).__mro__):
            for name in getattr(klass, "__annotations__", {}):
                if hasattr(klass, name):
                    v = getattr(klass, name)
                    setattr(self, name, dict(v) if isinstance(v, dict) else v)
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):
        return dict(vars(self))


def install(test, modules):
    p = mock.patch.dict(sys.modules, modules)
    p.start()
    test.addCleanup(p.stop)


class GymEnv(unittest.TestCase):
    def test_episode(self):
        from kosa8.rl.gym import SandboxEnv

        fake, cloud = sync_cloud(self)
        base = cloud.sandboxes.create()
        fake.on_exec = lambda sb, argv, env: {"exit": 0, "stdout": "ran " + argv[-1], "stderr": ""}
        env = SandboxEnv(cloud, base, [{"id": "a", "prompt": "do it", "files": {"/g.py": "x"}}],
                         grade_cmd=["python", "/g.py"], max_steps=3)
        obs, info = env.reset(seed=1)
        self.assertEqual(obs, "do it")
        self.assertNotEqual(info["sandbox_id"], base.id)
        obs, r, term, trunc, info = env.step("ls")
        self.assertEqual((obs, r, term, trunc), ("ran ls", 0.0, False, False))
        obs, r, term, trunc, info = env.step("submit")
        self.assertEqual((r, term, trunc, info["masked"]), (1.0, True, False, False))
        self.assertEqual(fake.live(), [base.id])
        with self.assertRaises(RuntimeError):
            env.step("ls")
        # max_steps truncates and grades; an infra failure masks.
        env.reset(options={"task": {"prompt": "p"}})
        env.step("a"), env.step("b")
        _, r, term, trunc, info = env.step("c")
        self.assertEqual((term, trunc), (False, True))
        env.reset()
        fake.on_exec = lambda sb, argv, env: (_ for _ in ()).throw(AssertionError)  # not reached
        fake.before = lambda m, p, b: (502, {"error": "host_failed", "message": "host lost"}) \
            if p.endswith("/exec") else None
        _, r, term, _, info = env.step("x")
        self.assertEqual((r, term, info["masked"], info["failure_class"]), (0.0, True, True, "infra"))
        env.close()
        self.assertEqual(fake.live(), [base.id])


class TrlFactory(unittest.TestCase):
    def test_environment_factory_contract(self):
        from kosa8.rl.trl import environment_factory, failure_classes, reward_func

        fake, cloud = sync_cloud(self)
        base = cloud.sandboxes.create()
        Env = environment_factory(lambda: cloud, base, grade_cmd=["python", "/grader/t.py"])
        self.assertEqual(len(inspect.signature(Env).parameters), 0)  # TRL calls it with no arguments
        tools = sorted(n for n in dir(Env) if not n.startswith("_") and callable(getattr(Env, n)) and n != "reset")
        self.assertEqual(tools, ["bash", "write_file"])
        for t in tools:
            self.assertIn("Args:", getattr(Env, t).__doc__)
        envs = [Env(), Env()]
        self.assertIsNone(envs[0].reset(prompt=[{"role": "user", "content": "x"}], files={"/grader/t.py": "x"}))
        self.assertEqual(envs[1].reset(prompt="solve it", files={"/grader/t.py": "x"}), "solve it")
        self.assertIn("[exit 0]", envs[0].bash("ls"))
        self.assertIn("wrote", envs[0].write_file("/workspace/s.py", "print(1)"))
        doomed = envs[1]._episode.box.id
        fake.before = lambda m, p, b: (503, {"error": "capacity", "message": "x"}) \
            if p.endswith(f"{doomed}/exec") else None
        with self.assertRaises(RuntimeError):
            envs[1].bash("ls")
        self.assertEqual(reward_func(envs), [1.0, None])  # None: TRL leaves it out of the loss
        self.assertEqual(failure_classes(envs), ["ok", "infra"])
        grades = [b for m, p, b in fake.calls if p.endswith("/grade")]
        self.assertEqual(len(grades), 1)
        self.assertEqual(reward_func(envs[:1]), [1.0])  # graded once
        self.assertEqual(fake.live(), [base.id])


def fake_openenv():
    es = types.ModuleType("openenv.core.env_server")

    class Action(_Model):
        metadata: dict = {}

    class Observation(_Model):
        done: bool = False
        reward: object = None
        metadata: dict = {}

    class State(_Model):
        episode_id: object = None
        step_count: int = 0

    class Environment:
        SUPPORTS_CONCURRENT_SESSIONS = False

        def __init__(self, transform=None, rubric=None):
            self.transform, self.rubric = transform, rubric

    def create_app(env, action_cls, observation_cls, env_name=None, max_concurrent_envs=None, **kw):
        return {"env": env, "action": action_cls, "obs": observation_cls, "max": max_concurrent_envs}

    es.Action, es.Observation, es.State, es.Environment, es.create_app = Action, Observation, State, Environment, create_app
    core = types.ModuleType("openenv.core")
    core.env_server = es
    root = types.ModuleType("openenv")
    root.core = core
    return {"openenv": root, "openenv.core": core, "openenv.core.env_server": es}


class OpenEnvAdapter(unittest.TestCase):
    def setUp(self):
        install(self, fake_openenv())
        import kosa8.rl.openenv as oe

        oe._CLASSES.clear()
        self.addCleanup(oe._CLASSES.clear)
        self.oe = oe

    def test_environment(self):
        fake, cloud = sync_cloud(self)
        base = cloud.sandboxes.create()
        env = self.oe.make_environment(cloud, base, [{"prompt": "go", "files": {"/g": "x"}}], grade_cmd=["true"])
        self.assertTrue(env.SUPPORTS_CONCURRENT_SESSIONS)
        obs = env.reset(seed=3, episode_id="ep1")
        self.assertEqual((obs.output, obs.done, env.state.episode_id), ("go", False, "ep1"))
        obs = env.step(self.oe.ShellAction(command="echo hi"))
        self.assertEqual((obs.output, obs.exit_code, obs.done, env.state.step_count), ("ok\n", 0, False, 1))
        obs = env.step(self.oe.ShellAction(submit=True))
        self.assertEqual((obs.done, obs.reward, obs.masked, obs.failure_class), (True, 1.0, False, "ok"))
        self.assertEqual(fake.live(), [base.id])
        app = self.oe.create_app(lambda: cloud, base, ["t"], grade_cmd=["true"], max_concurrent_envs=8)
        self.assertEqual(app["max"], 8)
        self.assertIs(app["action"], self.oe.ShellAction)
        self.assertIsInstance(app["env"](), self.oe.Kosa8ShellEnvironment)


def fake_verifiers():
    mods = {}

    def mod(name):
        m = types.ModuleType(name)
        mods[name] = m
        return m

    for n in ("verifiers", "verifiers.v1", "verifiers.v1.configs", "verifiers.v1.runtimes",
              "verifiers.v1.runtimes.backends"):
        mod(n)
    cfg = mod("verifiers.v1.configs.runtime")

    class CustomRuntimeConfig(_Model):
        type: str = "custom"
        import_path: str = ""

    cfg.CustomRuntimeConfig = CustomRuntimeConfig
    errs = mod("verifiers.v1.errors")

    class SandboxError(Exception):
        pass

    errs.SandboxError = SandboxError
    custom = mod("verifiers.v1.runtimes.backends.custom")

    class CustomRuntimeInfo(_Model):
        id: object = None

    custom.CustomRuntimeInfo = CustomRuntimeInfo
    base = mod("verifiers.v1.runtimes.base")

    @dataclasses.dataclass(frozen=True)
    class ProgramResult:
        exit_code: int
        stdout: str
        stderr: str

    class RuntimeProcess:
        pass

    class Runtime:
        def __init__(self, backend):
            self.backend, self.user = backend, None

        def process_env(self, env):
            return {**self.backend.env, **env}

    class RuntimeBackend:
        def __init__(self, name=None):
            self.name = name or "vf-test"
            self.env = {"VF": "1"}
            self.stopped = False

        def bind(self, *, env=None):
            return Runtime(self)

        async def stop(self):
            self.stopped = True
            await asyncio.to_thread(self.cleanup)

    registered = []
    base.ProgramResult, base.RuntimeProcess, base.RuntimeBackend = ProgramResult, RuntimeProcess, RuntimeBackend
    base.register = registered.append
    base.registered = registered
    return mods


class VerifiersAdapter(unittest.TestCase):
    def setUp(self):
        install(self, fake_verifiers())
        import kosa8.rl.verifiers as vf

        vf._CLASSES.clear()
        self.addCleanup(vf._CLASSES.clear)
        self.vf = vf
        self.fake = FakeCloud()
        p = mock.patch.object(vf, "cloud_factory",
                              lambda: AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=self.fake.aopen))
        p.start()
        self.addCleanup(p.stop)

    def test_runtime(self):
        vf = self.vf
        self.assertEqual(vf.wrap_argv(["ls", "-a"], "/w"), ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", "/w", "ls", "-a"])

        async def go():
            cfg = vf.Kosa8RuntimeConfig(import_path="kosa8.rl.verifiers.Kosa8Backend", image="ghcr.io/a/b:1")
            be = vf.Kosa8Backend(cfg)
            self.assertIs(type(be).config_class, vf.Kosa8RuntimeConfig)
            self.assertTrue(be.SUPPORTS_TASK_IMAGES)
            await be.start()
            box_id = be.info.id
            create = [b for m, p, b in self.fake.calls if p == "/v1/sandboxes" and m == "POST"][0]
            self.assertEqual((create["image"], create["cpus"], create["lease_s"]), ("ghcr.io/a/b:1", 2, 3600))
            self.assertEqual(sys.modules["verifiers.v1.runtimes.base"].registered, [be])
            rt = be.bind()
            r = await be.run(rt, ["echo", "hi"], {"X": "2"})
            self.assertEqual((r.exit_code, r.stdout), (0, "ok\n"))
            body = self.fake.calls[-1][2]
            self.assertEqual(body["cmd"][-2:], ["echo", "hi"])
            self.assertEqual(sorted(body["env"]), ["VF=1", "X=2"])
            proc = await be.exec(rt, ["ls"], {})
            out = b"".join([c async for c in proc.stdout])
            self.assertEqual((out, await proc.wait(), await proc.poll()), (b"ok\n", 0, 0))
            await be.write(rt, "notes.txt", b"abc")
            self.assertEqual(await be.read(rt, "notes.txt", max_bytes=1), b"ab")  # max + 1, as Runtime checks
            self.assertIn("/workspace/notes.txt", self.fake.sandboxes[box_id]["files"])
            await be.stop()
            self.assertNotIn(box_id, self.fake.live())
            be.cleanup()  # idempotent (also the atexit backstop)

        asyncio.run(go())


class NemoGymProvider(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud()
        self.cloud = AsyncCloud(KEY, env={}, base_url=LOCAL, async_opener=self.fake.aopen)

        async def no_wait(s):
            return None

        p = mock.patch("kosa8.retry._asleep", no_wait)
        p.start()
        self.addCleanup(p.stop)

    def spec(self, **kw):
        d = dict(image=None, ttl_s=None, ready_timeout_s=None, workdir=None, env={}, files={}, metadata={},
                 resources=types.SimpleNamespace(cpu=None, memory_mib=None, disk_gib=None, gpu=None),
                 entrypoint=None, provider_options={}, ports=(), sidecars=(), user=None)
        d.update(kw)
        return types.SimpleNamespace(**d)

    def test_provider(self):
        from kosa8.rl.nemo_gym import SANDBOX_RUNTIME_RETURN_CODE, Kosa8Provider

        prov = Kosa8Provider(cloud=self.cloud, create={"template": "python", "lease_s": 900})
        self.assertEqual(prov.name, "kosa8")
        fake = self.fake

        async def go():
            spec = self.spec(image="ghcr.io/acme/task:1", ttl_s=30, workdir="/app", env={"A": "1"},
                             files={"/app/in.txt": "data"}, metadata={"nemo-gym.nvidia.com/run": "r1"},
                             resources=types.SimpleNamespace(cpu=4.0, memory_mib=8192, disk_gib=None, gpu=None))
            h = await prov.create(spec)
            body = [b for m, p, b in fake.calls if p == "/v1/sandboxes" and m == "POST"][0]
            self.assertEqual((body["image"], body["cpus"], body["mem_mb"], body["ttl_s"], body["lease_s"]),
                             ("ghcr.io/acme/task:1", 4, 8192, 60, 900))
            self.assertNotIn("template", body)
            self.assertEqual(fake.sandboxes[h.sandbox_id]["labels"], {"nemo-gym.nvidia.com/run": "r1"})
            self.assertEqual(fake.sandboxes[h.sandbox_id]["files"]["/app/in.txt"], b"data")
            fake.on_exec = lambda sb, argv, env: {"exit": 2, "stdout": "o", "stderr": "e", "env": env}
            r = await prov.exec(h, "make test", env={"B": "2"})
            self.assertEqual((r.stdout, r.stderr, r.return_code), ("o", "e", 2))
            ex = [b for m, p, b in fake.calls if p.endswith("/exec")][-1]
            self.assertEqual(ex["cmd"], ["sh", "-c", "cd /app && make test"])
            self.assertEqual(sorted(ex["env"]), ["A=1", "B=2"])
            await prov.exec(h, "pwd", cwd="/tmp")
            self.assertEqual([b for m, p, b in fake.calls if p.endswith("/exec")][-1]["cmd"][-1], "cd /tmp && pwd")
            with TemporaryDirectory() as d:
                src = Path(d) / "a.txt"
                src.write_text("up")
                await prov.upload_file(h, src, "/app/a.txt")
                await prov.download_file(h, "/app/a.txt", Path(d) / "sub" / "b.txt")
                self.assertEqual((Path(d) / "sub" / "b.txt").read_text(), "up")
                with self.assertRaises(FileNotFoundError):
                    await prov.upload_file(h, Path(d) / "missing", "/x")
            self.assertEqual((await prov.status(h)).value, "running")
            snap = await prov.snapshot(h, name="s1")
            self.assertTrue(snap.startswith("snap_"))
            await prov.wait_snapshot_ready(snap)
            h2 = await prov.create_from_snapshot(snap, self.spec())
            body = [b for m, p, b in fake.calls if p == "/v1/sandboxes" and m == "POST"][-1]
            self.assertEqual(body, {"lease_s": 900, "snapshot": snap})
            # Timeouts raise; a vanished sandbox answers the runtime sentinel.
            fake.before = lambda m, p, b: (504, {"error": "exec_timeout", "message": "90 s"}) \
                if p.endswith("/exec") else None
            with self.assertRaises(TimeoutError):
                await prov.exec(h, "sleep 100")
            fake.before = None
            await prov.close(h2)
            r = await prov.exec(h2, "true")
            self.assertEqual((r.return_code, r.error_type), (SANDBOX_RUNTIME_RETURN_CODE, "sandbox"))
            self.assertEqual((await prov.status(h2)).value, "stopped")
            await prov.close(h2)  # already gone: fine
            for bad in (dict(ports=[8080]), dict(entrypoint=["x"]), dict(user="agent"),
                        dict(resources=types.SimpleNamespace(cpu=None, memory_mib=None, disk_gib=None, gpu=1))):
                with self.assertRaises(ValueError):
                    await prov.create(self.spec(**bad))
            with self.assertRaises(ValueError):
                await prov.exec(h, "id", user="root")
            await prov.close(h)
            await prov.aclose()
            self.assertEqual(fake.live(), [])

        asyncio.run(go())
        with self.assertRaises(ValueError):
            Kosa8Provider(cloud=self.cloud, create={"bogus": 1})

    def test_uses_nemo_gym_types_when_installed(self):
        base = types.ModuleType("nemo_gym.sandbox.providers.base")

        @dataclasses.dataclass
        class SandboxHandle:
            sandbox_id: str
            provider_name: str
            raw: object

        @dataclasses.dataclass(frozen=True)
        class SandboxExecResult:
            stdout: object
            stderr: object
            return_code: int
            error_type: object = None

        import enum

        class SandboxStatus(str, enum.Enum):
            STARTING = "starting"
            RUNNING = "running"
            PAUSED = "paused"
            STOPPED = "stopped"
            ERROR = "error"
            UNKNOWN = "unknown"

        base.SandboxHandle, base.SandboxExecResult, base.SandboxStatus = SandboxHandle, SandboxExecResult, SandboxStatus
        providers = types.ModuleType("nemo_gym.sandbox.providers")
        providers.base = base
        install(self, {"nemo_gym": types.ModuleType("nemo_gym"), "nemo_gym.sandbox": types.ModuleType("nemo_gym.sandbox"),
                       "nemo_gym.sandbox.providers": providers, "nemo_gym.sandbox.providers.base": base})
        from kosa8.rl.nemo_gym import Kosa8Provider

        prov = Kosa8Provider(cloud=self.cloud)

        async def go():
            h = await prov.create(self.spec())
            self.assertIsInstance(h, SandboxHandle)
            self.assertIsInstance(await prov.exec(h, "true"), SandboxExecResult)
            self.assertIs(await prov.status(h), SandboxStatus.RUNNING)
            await prov.close(h)

        asyncio.run(go())

    def test_entry_point_declared(self):
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        self.assertIn('[project.entry-points."nemo_gym.sandbox_providers"]', text)
        self.assertIn('kosa8 = "kosa8.rl.nemo_gym:Kosa8Provider"', text)


class SweBench(unittest.TestCase):
    INSTANCE = {"instance_id": "psf__requests-2317", "problem_statement": "fix it", "test_patch": "diff --git a b",
                "FAIL_TO_PASS": '["tests/test_x.py::test_a"]', "PASS_TO_PASS": ["tests/test_x.py::test_b"]}

    def test_names_files_tasks(self):
        from kosa8.rl import swebench

        self.assertEqual(swebench.instance_image(self.INSTANCE),
                         "docker.io/swebench/sweb.eval.x86_64.psf_1776_requests-2317:latest")
        files = swebench.grader_files(self.INSTANCE)
        self.assertEqual(files["/grader/test.patch"], "diff --git a b")
        run = files["/grader/run.sh"]
        self.assertIn("tests/test_x.py::test_a tests/test_x.py::test_b", run)
        self.assertIn("git diff --quiet HEAD", run)  # an empty patch is not a solve
        self.assertIn('{\\"reward\\": $r', run)
        custom = swebench.grader_files(self.INSTANCE, test_cmd="./tests/runtests.py {tests}")["/grader/run.sh"]
        self.assertIn("./tests/runtests.py tests/test_x.py::test_a", custom)
        (t,) = swebench.tasks([self.INSTANCE])
        self.assertEqual((t["id"], t["prompt"], sorted(t["files"])),
                         ("psf__requests-2317", "fix it", ["/grader/run.sh", "/grader/test.patch"]))

    def test_create_patch_grade(self):
        from kosa8.rl import swebench

        fake, cloud = sync_cloud(self)
        box = swebench.create(cloud, self.INSTANCE)
        body = [b for m, p, b in fake.calls if p == "/v1/sandboxes" and m == "POST"][0]
        self.assertEqual((body["image"], body["size"], body["persistent"]),
                         (swebench.instance_image(self.INSTANCE), "2x4", True))
        g = swebench.grade(box, self.INSTANCE, patch="--- a\n+++ b\n", run_id="run_s")
        self.assertEqual(g.reward, 1.0)
        self.assertEqual(fake.sandboxes[box.id]["files"]["/workspace/.kosa8/patch.diff"], b"--- a\n+++ b\n")
        grade = [b for m, p, b in fake.calls if p.endswith("/grade")][0]
        self.assertEqual((grade["cmd"], grade["run_id"], grade["timeout_s"]), (swebench.GRADE_CMD, "run_s", 90))
        fake.on_exec = lambda sb, argv, env: {"exit": 1, "stdout": "", "stderr": "error: patch failed"}
        with self.assertRaises(ValueError):
            swebench.grade(box, self.INSTANCE, patch="bad")
        with self.assertRaises(ValueError):
            swebench.apply_patch(box, "  ")


if __name__ == "__main__":
    unittest.main()
