# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
import json
import unittest
import urllib.parse
from unittest import mock

from kosa8 import CLOUD_SIZES, CloudSandbox, Kosa8Cloud, Kosa8Error, Kosa8TimeoutError, pipeline_run_done

KEY = "k8c_live_testkey"


def sbx(**over):
    d = {"id": "sbx_1", "state": "running", "template": "base", "cpus": 2, "mem_mb": 4096, "last_error": None}
    d.update(over)
    return d


SNAP = {"id": "snap_abcdefgh", "name": "base", "state": "ready", "source_sandbox_id": "sbx_1"}


class Stub:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        raw = headers.get("Content-Type") == "application/octet-stream"
        u = urllib.parse.urlsplit(url)
        s = {
            "method": method,
            "path": u.path,
            "query": urllib.parse.parse_qs(u.query),
            "headers": headers,
            "body": body if raw else (json.loads(body) if body else None),
        }
        self.seen.append(s)
        status, payload = self.answer(s)
        data = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        return status, data


def make(answer):
    stub = Stub(answer)
    return Kosa8Cloud(KEY, env={}, opener=stub), stub


def route(s):
    return f"{s['method']} {s['path']}"


def no_sleep():
    return mock.patch("time.sleep", lambda _s: None)


class UserAgent(unittest.TestCase):
    def test_sends_its_own_user_agent(self):
        # The API's edge refuses Python-urllib's default User-Agent (Cloudflare 1010).
        import kosa8

        cloud, stub = make(lambda s: (200, {}))
        cloud.me()
        self.assertEqual(stub.seen[0]["headers"]["User-Agent"], f"kosa8-python/{kosa8.__version__}")


class SizedCreate(unittest.TestCase):
    def test_sends_api_names(self):
        cloud, stub = make(lambda s: (201, {"sandbox": sbx()}))
        cloud.sandboxes.create(
            size="4x8", image="kosa8-template/universal-24.04-20261115.2", docker=True, disk_gb=64, cpu="dedicated",
            name="web-1", env={"NODE_ENV": "production"}, region="us", sleep_after_s=0,
            egress={"presets": ["npm"]}, secrets=["OPENAI"],
        )
        self.assertEqual(stub.seen[0]["body"], {
            "size": "4x8", "image": "kosa8-template/universal-24.04-20261115.2", "docker": True, "disk_gb": 64,
            "cpu": "dedicated", "name": "web-1", "env": {"NODE_ENV": "production"}, "region": "us",
            "sleep_after_s": 0, "egress": {"presets": ["npm"]}, "secrets": ["OPENAI"],
        })
        self.assertIn("16x32", CLOUD_SIZES)

    def test_cpus_mem_persistent(self):
        cloud, stub = make(lambda s: (201, {"sandbox": sbx()}))
        cloud.sandboxes.create(cpus=8, mem_mb=16384, persistent=True)
        self.assertEqual(stub.seen[0]["body"], {"cpus": 8, "mem_mb": 16384, "persistent": True})

    def test_checks_before_any_request(self):
        cloud, stub = make(lambda s: (201, {"sandbox": sbx()}))
        for kw, msg in (
            ({"cpus": 4}, "together"),
            ({"size": "4x8", "cpus": 4, "mem_mb": 8192}, "not both"),
            ({"docker": True, "persistent": False}, "persistent"),
            ({"disk_gb": 4}, "disk_gb"),
            ({"snapshot": "snap_abcdefgh", "size": "2x4", "template": "x"}, "template, size"),
        ):
            with self.assertRaisesRegex(ValueError, msg):
                cloud.sandboxes.create(**kw)
        self.assertEqual(stub.seen, [])

    def test_waits_for_cold_boot(self):
        gets = []

        def answer(s):
            if s["method"] == "POST":
                return 202, {"sandbox": sbx(state="creating")}
            gets.append(1)
            return 200, {"sandbox": sbx(state="creating" if len(gets) < 2 else "running")}

        cloud, stub = make(answer)
        with no_sleep():
            box = cloud.sandboxes.create(size="2x4")
        self.assertEqual(box.state, "running")
        self.assertEqual([route(s) for s in stub.seen], ["POST /v1/sandboxes", "GET /v1/sandboxes/sbx_1", "GET /v1/sandboxes/sbx_1"])

    def test_failed_boot_and_no_wait(self):
        cloud, _ = make(lambda s: (202, {"sandbox": sbx(state="creating")}) if s["method"] == "POST"
                        else (200, {"sandbox": sbx(state="error", last_error="no capacity")}))
        with no_sleep(), self.assertRaisesRegex(Kosa8Error, "no capacity"):
            cloud.sandboxes.create(size="2x4")
        self.assertEqual(cloud.sandboxes.create(size="2x4", wait=False).state, "creating")

    def test_wait_times_out(self):
        cloud, _ = make(lambda s: (202 if s["method"] == "POST" else 200, {"sandbox": sbx(state="creating")}))
        with no_sleep(), self.assertRaises(Kosa8TimeoutError):
            cloud.sandboxes.create(size="2x4", wait_timeout=0)

    def test_from_snapshot_and_labels(self):
        cloud, stub = make(lambda s: (201, {"sandbox": sbx(snapshot_id=SNAP["id"], labels={"team": "ml"})}))
        box = cloud.sandboxes.create(snapshot=SNAP, env={"A": "1"}, labels={"team": "ml"})
        # Labels go in the create body (stored before the answer), not a second call.
        self.assertEqual(stub.seen[0]["body"], {"snapshot": "snap_abcdefgh", "env": {"A": "1"}, "labels": {"team": "ml"}})
        self.assertEqual(len(stub.seen), 1)
        self.assertEqual(box.info["labels"], {"team": "ml"})

    def test_list_by_name_and_labels(self):
        cloud, stub = make(lambda s: (200, {"sandboxes": []}))
        cloud.sandboxes.list(name="web-1", labels={"team": "ml", "gpu": True})
        self.assertEqual(stub.seen[0]["query"], {"name": ["web-1"], "label": ["team=ml", "gpu"]})


class Background(unittest.TestCase):
    def test_start_and_wait(self):
        polls = []

        def answer(s):
            if s["method"] == "POST":
                return 202, {"exec": {"id": "bg_1", "sandbox_id": "sbx_1", "state": "running", "pid": 7, "exit": None}}
            polls.append(1)
            done = len(polls) >= 2
            return 200, {"exec": {"id": "bg_1", "state": "exited" if done else "running", "exit": 0 if done else None, "output": "built\n"}}

        cloud, stub = make(answer)
        box = CloudSandbox(cloud, sbx())
        job = box.exec_background("npm run build", env={"CI": "1"})
        self.assertEqual(stub.seen[0]["body"], {"cmd": ["sh", "-c", "npm run build"], "background": True, "env": ["CI=1"]})
        self.assertEqual(job.id, "bg_1")
        with no_sleep():
            done = job.wait()
        self.assertEqual((done["state"], done["exit"], done["output"]), ("exited", 0, "built\n"))
        self.assertEqual(route(stub.seen[1]), "GET /v1/sandboxes/sbx_1/exec/bg_1")

    def test_status_nulls_keep_what_the_start_said(self):
        # The status read has no pid, and the API answers missing fields as
        # null: id and pid must survive it, so stop() still has a path.
        def answer(s):
            if s["method"] == "POST" and s["path"].endswith("/exec"):
                return 202, {"exec": {"id": "bg_1", "state": "running", "pid": 7, "output_path": "/o", "exit_path": "/e"}}
            if s["path"].endswith("/stop"):
                return 200, {"exec": {"id": "bg_1", "state": "exited", "exit": 143, "stopped": True}}
            return 200, {"exec": {"id": None, "state": "running", "pid": None, "exit": None, "output_path": None, "exit_path": None}}

        cloud, stub = make(answer)
        job = CloudSandbox(cloud, sbx()).exec_background("sleep 9")
        info = job.status()
        self.assertEqual((info["id"], info["pid"], info["output_path"], info["exit_path"]), ("bg_1", 7, "/o", "/e"))
        self.assertEqual(job.stop()["state"], "exited")
        self.assertEqual(route(stub.seen[-1]), "POST /v1/sandboxes/sbx_1/exec/bg_1/stop")

    def test_get_exec_and_timeout(self):
        cloud, _ = make(lambda s: (200, {"exec": {"id": "bg_2", "state": "running", "pid": 1, "exit": None}}))
        job = CloudSandbox(cloud, sbx()).get_exec("bg_2")
        self.assertEqual(job.info["pid"], 1)
        with no_sleep(), self.assertRaises(Kosa8TimeoutError):
            job.wait(timeout=0)


class Snapshots(unittest.TestCase):
    def test_lifecycle(self):
        def answer(s):
            if s["method"] == "POST":
                return 201, {"snapshot": SNAP, "ms": 900}
            if s["method"] == "DELETE":
                return 200, {"ok": True, "id": SNAP["id"]}
            if s["path"] == "/v1/snapshots":
                return 200, {"snapshots": [SNAP], "max": 50}
            return 200, {"snapshot": SNAP}

        cloud, stub = make(answer)
        box = CloudSandbox(cloud, sbx())
        self.assertEqual(box.snapshot("base")["id"], SNAP["id"])
        self.assertEqual(stub.seen[0]["body"], {"name": "base"})
        self.assertEqual(len(cloud.snapshots.list(sandbox_id="sbx_1")), 1)
        self.assertEqual(stub.seen[1]["query"], {"sandbox_id": ["sbx_1"]})
        self.assertEqual(cloud.snapshots.get(SNAP["id"])["state"], "ready")
        cloud.snapshots.delete(SNAP["id"])
        cloud.snapshots.create("sbx_1")
        self.assertEqual([route(s) for s in stub.seen[2:]], [
            "GET /v1/snapshots/snap_abcdefgh", "DELETE /v1/snapshots/snap_abcdefgh", "POST /v1/sandboxes/sbx_1/snapshots",
        ])


class Previews(unittest.TestCase):
    def test_previews(self):
        p = {"port": 3000, "public": False, "url": "https://abc.kosa8labs.com/?t=x", "host": "abc.kosa8labs.com"}

        def answer(s):
            if s["path"].endswith("/previews") and s["method"] == "GET":
                return 200, {"previews": [p], "configured": True}
            if s["method"] == "DELETE":
                return 200, {"deleted": True, "port": 3000}
            return 200, {"preview": p}

        cloud, stub = make(answer)
        box = CloudSandbox(cloud, sbx())
        self.assertIn("kosa8labs.com", box.previews.create(3000, public=False, expires_in_s=600, wake=True, wake_per_hour=5)["url"])
        self.assertEqual(stub.seen[0]["body"], {"port": 3000, "public": False, "expires_in_s": 600, "wake": True, "wake_per_hour": 5})
        self.assertEqual(len(box.previews.list()), 1)
        box.previews.link(3000, expires_in_s=60)
        self.assertEqual(stub.seen[2]["body"], {"expires_in_s": 60})
        box.previews.rotate(3000)
        box.previews.delete(3000)
        self.assertEqual([route(s) for s in stub.seen[2:]], [
            "POST /v1/sandboxes/sbx_1/previews/3000/link", "POST /v1/sandboxes/sbx_1/previews/3000/rotate",
            "DELETE /v1/sandboxes/sbx_1/previews/3000",
        ])
        with self.assertRaisesRegex(ValueError, "port"):
            box.previews.create(0)


class Secrets(unittest.TestCase):
    def test_value_in_body_only(self):
        def answer(s):
            if s["method"] == "PUT":
                return 201, {"secret": {"name": "OPENAI", "upstream": "api.openai.com", "pipelines": True}, "replaced": False}
            if s["method"] == "DELETE":
                return 200, {"deleted": "OPENAI", "still_held_by": ["sbx_1"]}
            return 200, {"secrets": [{"name": "OPENAI"}]}

        cloud, stub = make(answer)
        r = cloud.secrets.set("OPENAI", "sk-secret", upstream="api.openai.com", pipelines=True)
        self.assertEqual(stub.seen[0]["path"], "/v1/secrets/OPENAI")
        self.assertEqual(stub.seen[0]["query"], {})
        self.assertEqual(stub.seen[0]["body"], {"value": "sk-secret", "upstream": "api.openai.com", "pipelines": True})
        self.assertNotIn("sk-secret", json.dumps(r))
        self.assertEqual(cloud.secrets.list(), [{"name": "OPENAI"}])
        self.assertEqual(cloud.secrets.delete("OPENAI"), {"deleted": "OPENAI", "still_held_by": ["sbx_1"]})
        with self.assertRaisesRegex(ValueError, "value"):
            cloud.secrets.set("X", "")
        self.assertNotIn("sk-secret", repr(cloud))


class SandboxExtras(unittest.TestCase):
    def test_everything_else(self):
        eg = {"internet": False, "allow": ["pypi.org"], "deny": [], "presets": [], "source": "sandbox", "enforced": "host"}

        def answer(s):
            p, m = s["path"], s["method"]
            if p.endswith("/egress"):
                return 200, {"egress": eg}
            if p.endswith("/terminal"):
                return 200, {"terminal": {"url": "wss://x", "expires_at": 1, "protocol": "kosa8-pty"}}
            if p.endswith("/code"):
                return 200, {"context": "default", "execution_count": 1, "results": [{"type": "text", "data": "42"}], "stdout": "", "stderr": ""}
            if p.endswith("/code/contexts") and m == "GET":
                return 200, {"contexts": [{"name": "default", "execution_count": 1}]}
            if p.endswith("/code/contexts"):
                return 201, {"context": {"name": "nb", "execution_count": 0}}
            if "/code/contexts/" in p:
                return 200, {}
            if p.endswith("/schedules") and m == "POST":
                return 201, {"schedule": {"id": "sch_1", "cron": "0 * * * *"}}
            if p.endswith("/schedules"):
                return 200, {"schedules": []}
            if p.endswith("/run"):
                return 200, {"schedule": {}, "run": {"status": "ok", "exit": 0}}
            if "/schedules/" in p:
                return 200, {"deleted": True}
            if p.endswith("/metrics"):
                return 200, {"metrics": {"cpu_pct": 3}}
            if p.endswith("/logs"):
                return 200, {"id": "sbx_1", "entries": [{"cmd": ["ls"], "exit": 0}]}
            if p.endswith("/secrets"):
                return 200, {"id": "sbx_1", "secrets": ["OPENAI"]}
            return 404, {"error": "not_found"}

        cloud, stub = make(answer)
        box = CloudSandbox(cloud, sbx())
        self.assertEqual(box.set_egress(internet=False, allow=["pypi.org"])["allow"], ["pypi.org"])
        self.assertEqual(stub.seen[0]["body"], {"internet": False, "allow": ["pypi.org"]})
        box.egress()
        box.reset_egress()
        self.assertEqual(box.terminal(cols=80, rows=24)["url"], "wss://x")
        self.assertEqual(stub.seen[3]["body"], {"cols": 80, "rows": 24})
        run = box.run_code("6*7", context="nb", timeout_s=5)
        self.assertEqual(stub.seen[4]["body"], {"code": "6*7", "context": "nb", "timeout_s": 5})
        self.assertEqual(run["results"][0]["data"], "42")
        self.assertEqual(len(box.code_contexts.list()), 1)
        self.assertEqual(box.code_contexts.create("nb")["name"], "nb")
        box.code_contexts.reset("nb")
        box.code_contexts.delete("nb")
        box.schedules.create("0 * * * *", "date", env={"TZ": "UTC"}, wake=True)
        self.assertEqual(stub.seen[9]["body"], {"cron": "0 * * * *", "cmd": ["sh", "-c", "date"], "env": ["TZ=UTC"], "wake": True})
        box.schedules.list()
        self.assertEqual(box.schedules.run("sch_1")["run"]["exit"], 0)
        box.schedules.delete("sch_1")
        self.assertEqual(box.metrics()["cpu_pct"], 3)
        self.assertEqual(len(box.logs(limit=5)), 1)
        self.assertEqual(stub.seen[14]["query"], {"limit": ["5"]})
        self.assertEqual(box.secrets(), ["OPENAI"])
        self.assertEqual([route(s) for s in stub.seen], [
            "PUT /v1/sandboxes/sbx_1/egress", "GET /v1/sandboxes/sbx_1/egress", "DELETE /v1/sandboxes/sbx_1/egress",
            "POST /v1/sandboxes/sbx_1/terminal", "POST /v1/sandboxes/sbx_1/code", "GET /v1/sandboxes/sbx_1/code/contexts",
            "POST /v1/sandboxes/sbx_1/code/contexts", "POST /v1/sandboxes/sbx_1/code/contexts/nb/reset",
            "DELETE /v1/sandboxes/sbx_1/code/contexts/nb", "POST /v1/sandboxes/sbx_1/schedules",
            "GET /v1/sandboxes/sbx_1/schedules", "POST /v1/sandboxes/sbx_1/schedules/sch_1/run",
            "DELETE /v1/sandboxes/sbx_1/schedules/sch_1", "GET /v1/sandboxes/sbx_1/metrics", "GET /v1/sandboxes/sbx_1/logs",
            "GET /v1/sandboxes/sbx_1/secrets",
        ])
        with self.assertRaisesRegex(ValueError, "cron"):
            box.schedules.create("", "x")

    def test_raw_files_and_links(self):
        def answer(s):
            if s["path"].endswith("/files/link"):
                return 200, {"link": {"url": "https://h/x", "method": "PUT", "path": "/workspace/big", "expires_at": 1}}
            if s["method"] == "PUT":
                return 200, {"file": {"path": "/workspace/big", "size": 3}}
            return 200, b"\x01\x02\x03"

        cloud, stub = make(answer)
        box = CloudSandbox(cloud, sbx())
        self.assertEqual(box.files.read_raw("/workspace/big"), b"\x01\x02\x03")
        self.assertEqual(box.files.write_raw("/workspace/big", "abc")["size"], 3)
        self.assertEqual(stub.seen[1]["body"], b"abc")
        self.assertEqual(box.files.link("/workspace/big", method="PUT", ttl_s=60)["method"], "PUT")
        self.assertEqual(stub.seen[2]["body"], {"path": "/workspace/big", "method": "PUT", "ttl_s": 60})
        self.assertEqual([(route(s), s["query"]) for s in stub.seen[:2]], [
            ("GET /v1/sandboxes/sbx_1/files/raw", {"path": ["/workspace/big"]}),
            ("PUT /v1/sandboxes/sbx_1/files/raw", {"path": ["/workspace/big"]}),
        ])
        with self.assertRaises(ValueError):
            box.files.link("/x", ttl_s=1000)


class OrgLevel(unittest.TestCase):
    def test_egress_and_capabilities(self):
        def answer(s):
            if s["path"] == "/v1/egress/presets":
                return 200, {"presets": {"pypi": ["pypi.org"]}}
            if s["path"] == "/v1/org/egress":
                return 200, {"egress": None, "applied": 0, "failed": []}
            return 200, {"features": {}}

        cloud, stub = make(answer)
        self.assertEqual(cloud.egress_presets(), {"pypi": ["pypi.org"]})
        self.assertIsNone(cloud.org_egress())
        cloud.set_org_egress({"internet": False, "presets": ["pypi"]}, enforce=True)
        self.assertEqual(stub.seen[2]["body"], {"default": {"internet": False, "presets": ["pypi"]}, "enforce": True})
        cloud.set_org_egress(None)
        self.assertEqual(stub.seen[3]["body"], {"default": None})
        cloud.capabilities()
        self.assertEqual(route(stub.seen[4]), "GET /v1/capabilities")


class Pipelines(unittest.TestCase):
    def test_runs(self):
        gets = []

        def run(state):
            return {"id": "run_1", "state": state, "finished_at": None}

        def answer(s):
            p = s["path"]
            if p == "/v1/pipelines/runs" and s["method"] == "POST":
                return 201, {"run": run("queued")}
            if p == "/v1/pipelines/runs":
                return 200, {"runs": [run("running")]}
            if p.endswith("/logs"):
                return 200, {"run_id": "run_1", "state": "running", "done": False, "logs": [{"job": "test", "text": "ok", "next": 2}]}
            if p.endswith("/cancel"):
                return 200, {"run": run("cancelled")}
            if p.endswith("/artifacts"):
                return 200, {"artifacts": [{"job": "build", "idx": 0, "path": "dist", "bytes": 3, "url": "/x"}]}
            if "/artifacts/" in p:
                return 200, b"\x1f\x8b"
            gets.append(1)
            return 200, {"run": run("running" if len(gets) < 2 else "success")}

        cloud, stub = make(answer)
        cloud.pipelines.run("https://github.com/a/b", ref="main", size="4x8", secrets=["NPM"], token_secret="GH", timeout_s=600)
        self.assertEqual(stub.seen[0]["body"], {"repo_url": "https://github.com/a/b", "ref": "main", "size": "4x8", "secrets": ["NPM"], "token_secret": "GH", "timeout_s": 600})
        cloud.pipelines.list(state="running", limit=5)
        self.assertEqual(stub.seen[1]["query"], {"state": ["running"], "limit": ["5"]})
        self.assertEqual(cloud.pipelines.logs("run_1", job="test", after=10)["logs"][0]["next"], 2)
        self.assertEqual(stub.seen[2]["query"], {"job": ["test"], "after": ["10"]})
        self.assertEqual(len(cloud.pipelines.artifacts("run_1")), 1)
        self.assertEqual(cloud.pipelines.download_artifact("run_1", "build", 0), b"\x1f\x8b")
        self.assertEqual(stub.seen[4]["path"], "/v1/pipelines/runs/run_1/artifacts/build/0")
        self.assertEqual(cloud.pipelines.cancel("run_1")["state"], "cancelled")
        with no_sleep():
            self.assertEqual(cloud.pipelines.wait("run_1")["state"], "success")
        self.assertTrue(pipeline_run_done({"state": "failure"}))
        self.assertFalse(pipeline_run_done({"state": "running", "finished_at": None}))
        with self.assertRaisesRegex(ValueError, "repo_url"):
            cloud.pipelines.run("")

    def test_hooks(self):
        def answer(s):
            if s["method"] == "POST":
                return 201, {"hook": {"id": "hk_1", "url": "https://api.kosa8.com/v1/pipelines/hooks/hk_1", "secret": "whsec"}}
            if s["method"] == "DELETE":
                return 200, {"deleted": "hk_1"}
            return 200, {"hooks": []}

        cloud, stub = make(answer)
        h = cloud.pipelines.hooks.create("https://github.com/a/b", provider="github", cron="0 3 * * *", cron_ref="main")
        self.assertEqual(stub.seen[0]["body"], {"repo_url": "https://github.com/a/b", "provider": "github", "cron": "0 3 * * *", "cron_ref": "main"})
        self.assertEqual(h["secret"], "whsec")
        cloud.pipelines.hooks.list()
        cloud.pipelines.hooks.delete("hk_1")
        self.assertEqual([route(s) for s in stub.seen], ["POST /v1/pipelines/hooks", "GET /v1/pipelines/hooks", "DELETE /v1/pipelines/hooks/hk_1"])


if __name__ == "__main__":
    unittest.main()


class OrgTemplates(unittest.TestCase):
    def test_build_import_get_wait_delete(self):
        states = iter(["building", "building", "ready"])

        def answer(s):
            r = route(s)
            if r == "POST /v1/templates":
                return 202, {"template": {"id": "tpl_1", "name": s["body"]["name"], "state": "building"}}
            if r == "POST /v1/templates/import":
                return 202, {"import_id": "imp_1", "queued": 2, "templates": [], "failed": []}
            if r == "GET /v1/templates/swe%2Fx" or r == "GET /v1/templates/swe/x":
                return 200, {"template": {"name": "swe/x", "state": next(states), "log": "ok"}}
            if r == "GET /v1/templates":
                return 200, {"templates": [{"key": "base"}], "org_templates": [{"name": "swe/x"}]}
            if r.startswith("DELETE /v1/templates/"):
                return 200, {"ok": True, "id": "tpl_1"}
            return 404, {"error": "not_found"}

        cloud, stub = make(answer)
        t = cloud.org_templates.build("df", dockerfile="FROM python:3.12-slim\n", cpus=2, build_args={"V": "1"})
        self.assertEqual(t["state"], "building")
        self.assertEqual(stub.seen[-1]["body"], {"name": "df", "dockerfile": "FROM python:3.12-slim\n", "cpus": 2, "build_args": ["V=1"]})
        with self.assertRaisesRegex(ValueError, "one of them"):
            cloud.org_templates.build("x")
        with self.assertRaisesRegex(ValueError, "one of them"):
            cloud.org_templates.build("x", image="a", dockerfile="FROM a")
        imp = cloud.org_templates.import_images({"a": "alpine@sha256:" + "0" * 64, "b": "python:3.12"}, mem_mb=2048)
        self.assertEqual(imp["queued"], 2)
        self.assertEqual(stub.seen[-1]["body"], {"images": [{"name": "a", "image": "alpine@sha256:" + "0" * 64}, {"name": "b", "image": "python:3.12"}], "partial_ok": True, "mem_mb": 2048})
        with no_sleep():
            self.assertEqual(cloud.org_templates.wait("swe/x")["state"], "ready")
        self.assertEqual(stub.seen[-1]["path"], "/v1/templates/swe%2Fx")
        self.assertEqual(cloud.org_templates.list(), [{"name": "swe/x"}])
        self.assertEqual(cloud.templates(), [{"key": "base"}], "the built-in list is unchanged")
        cloud.org_templates.delete("tpl_1")
        self.assertEqual(route(stub.seen[-1]), "DELETE /v1/templates/tpl_1")

    def test_wait_raises_on_failed_with_the_log(self):
        cloud, _ = make(lambda s: (200, {"template": {"name": "x", "state": "failed", "log": "pull: manifest 404"}}))
        from kosa8 import TemplateBuildError
        with self.assertRaisesRegex(TemplateBuildError, "manifest 404"):
            cloud.org_templates.wait("x")


class ForkPrefix(unittest.TestCase):
    def test_prefix_is_sent_and_checked(self):
        def answer(s):
            if s["path"].endswith("/fork"):
                n = s["body"]["n"]
                return 201, {"sandboxes": [{"id": f"sbx_c{i}", "state": "running", "name": f"{s['body'].get('prefix')}-{i + 1:04d}"} for i in range(n)], "failed": []}
            return 200, {"sandbox": {"id": "sbx_p", "state": "running"}}

        cloud, stub = make(answer)
        box = cloud.sandboxes.get("sbx_p")
        kids = box.fork(3, prefix="fk", run_id="run_1")
        self.assertEqual(stub.seen[-1]["body"], {"n": 3, "prefix": "fk", "run_id": "run_1"})
        self.assertEqual(len(kids), 3)
        with self.assertRaisesRegex(ValueError, "prefix"):
            box.fork(2, prefix="-bad")
