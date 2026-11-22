"""An in-memory kosa8 Cloud API for tests: an opener (sync or async) that
keeps sandboxes, labels, leases, files and RL runs, with hooks to inject
failures. It speaks the shapes of the RL spec (batch, fork options,
heartbeat, drain, grade, /v1/rl/runs)."""

import base64
import itertools
import json
import threading
import urllib.parse


class FakeCloud:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.sandboxes = {}  # id -> dict(info..., files={}, labels={})
        self.deleted = []
        self.calls = []  # (method, path, body)
        self.runs = {}
        self.rollouts = []
        self.heartbeats = []
        self.drains = []
        # Hooks: return a (status, body[, headers]) to answer instead, or None.
        self.before = None
        # exec(sandbox, argv, env) -> dict answer (exit/stdout/stderr)
        self.on_exec = lambda sb, argv, env: {"exit": 0, "stdout": "ok\n", "stderr": ""}
        # grade(sandbox, body, files) -> dict answer
        self.on_grade = None
        self.create_state = "running"
        self.running_limit = None

    # -- helpers ------------------------------------------------------------

    def live(self):
        with self.lock:
            return sorted(self.sandboxes)

    def _new(self, **info):
        sid = f"sbx_{next(self.ids)}"
        sb = {"id": sid, "state": self.create_state, "labels": {}, "files": {}, **info}
        self.sandboxes[sid] = sb
        return sb

    @staticmethod
    def _public(sb):
        return {k: v for k, v in sb.items() if k != "files"}

    def _match(self, body):
        ids = set(body.get("ids") or [])
        labels = dict(body.get("labels") or {})
        if body.get("run_id"):
            labels["kosa8.run"] = body["run_id"]
        out = []
        for sb in self.sandboxes.values():
            if ids and sb["id"] not in ids:
                continue
            if any(sb["labels"].get(k) != v for k, v in labels.items()):
                continue
            out.append(sb)
        return out

    # -- the opener ---------------------------------------------------------

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        path, query = u.path, dict(urllib.parse.parse_qsl(u.query))
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), None)
        raw = ctype == "application/octet-stream"
        parsed = body if raw else (json.loads(body) if body else None)
        with self.lock:
            self.calls.append((method, path, parsed))
        if self.before is not None:
            hooked = self.before(method, path, parsed)
            if hooked is not None:
                return self._encode(*hooked)
        with self.lock:
            out = self._route(method, path, query, parsed)
        return self._encode(*out)

    async def aopen(self, method, url, headers, body, timeout):
        return self(method, url, headers, body, timeout)

    @staticmethod
    def _encode(status, payload, headers=None):
        if isinstance(payload, bytes):
            data = payload
        else:
            data = b"" if payload is None else json.dumps(payload).encode()
        return (status, data, headers or {})

    def _route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if parts[:2] == ["v1", "rl"]:
            return self._rl(method, parts[2:], query, body)
        if parts[:2] != ["v1", "sandboxes"]:
            return 404, {"error": "not_found", "message": path}
        rest = parts[2:]
        if not rest:
            if method == "POST":
                if self.running_limit is not None and len(self.sandboxes) >= self.running_limit:
                    return 429, {"error": "running_limit", "message": "at the running limit",
                                 "failure_class": "quota", "retryable": False}
                info = {k: v for k, v in (body or {}).items() if k in ("template", "image", "lease_s", "snapshot")}
                return 201, {"sandbox": self._public(self._new(**info))}
            if method == "GET":
                sel = query.get("label")
                want = dict([sel.split("=", 1)]) if sel and "=" in sel else {}
                return 200, {"sandboxes": [self._public(s) for s in self.sandboxes.values()
                                           if all(s["labels"].get(k) == v for k, v in want.items())]}
        if rest == ["batch"]:
            n = body["count"]
            made, failed = [], []
            for i in range(n):
                if self.running_limit is not None and len(self.sandboxes) >= self.running_limit:
                    failed.append({"index": i, "code": "running_limit", "message": "at the running limit",
                                   "failure_class": "quota", "retryable": False})
                    continue
                sb = self._new(lease_s=body.get("lease_s"))
                sb["labels"] = dict(body.get("labels") or {})
                if body.get("run_id"):
                    sb["labels"]["kosa8.run"] = body["run_id"]
                made.append(self._public(sb))
            if failed and not body.get("partial_ok", True):
                for s in made:
                    self.sandboxes.pop(s["id"], None)
                return 429, {"error": "running_limit", "message": "at the running limit"}
            return 200, {"sandboxes": made, "failed": failed, "requested": n, "created": len(made)}
        if rest == ["heartbeat"]:
            self.heartbeats.append(body)
            hits = [s for s in self._match(body) if s.get("lease_s")]
            return 200, {"extended": len(hits)}
        if rest == ["drain"]:
            self.drains.append(body)
            hits = self._match(body)
            for s in hits:
                self.sandboxes.pop(s["id"], None)
                self.deleted.append(s["id"])
            return 200, {"deleted": len(hits), "failed": []}
        sid = rest[0]
        sb = self.sandboxes.get(sid)
        if sb is None:
            return 404, {"error": "not_found", "message": f"no sandbox {sid}"}
        sub = rest[1:]
        if not sub:
            if method == "DELETE":
                self.sandboxes.pop(sid, None)
                self.deleted.append(sid)
                return 200, {"deleted": True}
            if method == "GET":
                if sb["state"] == "creating":
                    sb["state"] = sb.get("next_state", "running")
                return 200, {"sandbox": self._public(sb)}
        if sub == ["labels"]:
            sb["labels"] = dict(body["labels"])
            return 200, {"labels": sb["labels"]}
        if sub == ["exec"]:
            env = dict(e.split("=", 1) for e in (body.get("env") or []))
            return 200, self.on_exec(sb, body["cmd"], env)
        if sub == ["files"]:
            p = query["path"]
            if method == "PUT":
                sb["files"][p] = body
                return 200, {"file": {"path": p, "size": len(body)}}
            if method == "GET":
                if p not in sb["files"]:
                    return 404, {"error": "not_found", "message": p}
                return 200, sb["files"][p]
            if method == "DELETE":
                sb["files"].pop(p, None)
                return 200, {}
        if sub == ["files", "list"]:
            return 200, {"files": [{"path": k} for k in sb["files"]]}
        if sub == ["fork"]:
            n = body["n"]
            kids = []
            for _ in range(n):
                k = self._new(parent_id=sid, lease_s=body.get("lease_s") or sb.get("lease_s"))
                k["files"] = dict(sb["files"])
                k["labels"] = dict(body.get("labels") or {})
                if body.get("run_id"):
                    k["labels"]["kosa8.run"] = body["run_id"]
                kids.append(self._public(k))
            return 200, {"sandboxes": kids}
        if sub == ["grade"]:
            files = {p: base64.b64decode(v) for p, v in (body.get("files") or {}).items()}
            if self.on_grade is not None:
                ans = self.on_grade(sb, body, files)
            else:
                ans = {"reward": 1.0, "metrics": {}, "exit": 0, "stdout": '{"reward": 1.0}\n', "stderr": "",
                       "failure_class": "ok", "ms": 5, "grader_id": f"sbx_g{next(self.ids)}"}
            if body.get("run_id") and isinstance(ans, dict) and "reward" in ans:
                ro = dict(body.get("rollout") or {})
                self.rollouts.append({"run_id": body["run_id"], "sandbox_id": sid, "reward": ans["reward"],
                                      "failure_class": ans.get("failure_class"), "from": "grade", **ro})
            if isinstance(ans, tuple):
                return ans
            return 200, ans
        if sub == ["snapshots"]:
            return 201, {"snapshot": {"id": f"snap_{next(self.ids)}", "name": (body or {}).get("name")}}
        return 404, {"error": "not_found", "message": path}

    def _rl(self, method, parts, query, body):
        if parts == ["runs"]:
            if method == "POST":
                rid = f"run_{next(self.ids)}"
                self.runs[rid] = {"id": rid, "status": "running", **(body or {})}
                return 201, {"run": self.runs[rid]}
            return 200, {"runs": list(self.runs.values())}
        rid = parts[1]
        run = self.runs.get(rid)
        if run is None:
            return 404, {"error": "not_found", "message": rid}
        if parts[2:] == []:
            if method == "PATCH":
                run.update(body)
                return 200, {"run": run}
            return 200, {"run": run, "summary": {"rollouts": len([r for r in self.rollouts if r["run_id"] == rid])}}
        if parts[2:] == ["rollouts"]:
            if method == "POST":
                rows = body["rollouts"]
                assert len(rows) <= 500
                for r in rows:
                    self.rollouts.append({"run_id": rid, "from": "sdk", **r})
                return 200, {"recorded": len(rows)}
            rows = [r for r in self.rollouts if r["run_id"] == rid]
            return 200, {"rollouts": rows, "next_cursor": None, "query": query}
        if parts[2:] == ["export"]:
            rows = [r for r in self.rollouts if r["run_id"] == rid]
            return 200, ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        return 404, {"error": "not_found", "message": "/".join(parts)}
