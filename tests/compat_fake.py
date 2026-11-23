"""An in-memory kosa8 Cloud API for the compat shim tests: an ``opener`` for
:class:`kosa8.Kosa8Cloud` that answers the endpoints the shims call, keeps
state between calls and records every request. No network."""

import base64
import json
import posixpath
import re
import urllib.parse

from kosa8 import Kosa8Cloud

KEY = "k8c_live_testkey"


class FakeCloud:
    def __init__(self):
        self.seen = []  # (method, path, query dict, body)
        self.boxes = {}
        self.files = {}  # (sandbox id, path) -> bytes
        self.dirs = set()  # (sandbox id, path)
        self.snaps = {}
        self.bg = {}
        self.n = 0
        #: argv -> (exit, stdout, stderr); default echoes argv[-1]
        self.on_exec = None
        #: argv, env -> list of stream frames for a streamed exec, or None
        self.on_stream = None
        self.bg_polls_until_exit = 1
        #: a background command's output poll by poll; it exits on the last
        self.bg_progress = None
        self.code_answer = {"execution_count": 1, "results": [], "stdout": "", "stderr": ""}
        self.fail = {}  # "METHOD /path" -> (status, body)
        #: persistent sandboxes the plan has left (None: no limit)
        self.persistent_left = None

    def cloud(self):
        return Kosa8Cloud(KEY, base_url="http://localhost:8787", env={}, opener=self)

    # --- helpers for tests ---
    def routes(self):
        return [f"{m} {p}" for m, p, _, _ in self.seen]

    def last(self, prefix):
        for m, p, q, b in reversed(self.seen):
            if f"{m} {p}".startswith(prefix):
                return {"method": m, "path": p, "query": q, "body": b}
        raise AssertionError(f"no request {prefix!r} in {self.routes()}")

    def _id(self, kind):
        self.n += 1
        return f"{kind}_{self.n}"

    def add_box(self, **over):
        sid = over.pop("id", None) or self._id("sbx")
        self.boxes[sid] = {"id": sid, "state": "running", "template": "base", "cpus": 1, "mem_mb": 512, "labels": {}, **over}
        return sid

    # --- the opener ---
    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        path, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        self._labels = [v for k, v in urllib.parse.parse_qsl(u.query) if k == "label"]
        raw = headers.get("Content-Type") == "application/octet-stream"
        b = body if raw else (json.loads(body) if body else None)
        self.seen.append((method, path, q, b))
        key = f"{method} {path}"
        if key in self.fail:
            st, payload = self.fail[key]
            return st, json.dumps(payload).encode(), {}
        st, payload = self.answer(method, path, q, b)
        data = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        return st, data, {}

    def answer(self, method, path, q, b):
        nf = (404, {"error": "not_found", "message": "no such thing"})
        if path == "/v1/sandboxes" and method == "POST":
            if b.get("persistent") and self.persistent_left is not None:
                if self.persistent_left <= 0:
                    return 402, {"error": "plan_limit", "message": "Solo includes 1 persistent sandbox. This organization has used 1",
                                 "limit_name": "persistent", "limit": 1, "used": 1}
                self.persistent_left -= 1
            info = {k: v for k, v in b.items() if k not in ("env", "secrets")}
            if "snapshot" in b:
                info["template"] = self.snaps.get(b["snapshot"], {}).get("template", "base")
            sid = self.add_box(**info)
            return 201, {"sandbox": self.boxes[sid]}
        if path == "/v1/sandboxes" and method == "GET":
            out = list(self.boxes.values())
            if "name" in q:
                out = [s for s in out if s.get("name") == q["name"]]
            for lab in self._labels:
                k, _, v = lab.partition("=")
                out = [s for s in out if s.get("labels", {}).get(k) == v]
            return 200, {"sandboxes": out}
        if path == "/v1/snapshots" and method == "GET":
            out = [s for s in self.snaps.values() if s["state"] != "deleted"]
            if q.get("sandbox_id"):
                out = [s for s in out if s["source_sandbox_id"] == q["sandbox_id"]]
            return 200, {"snapshots": out}
        m = re.match(r"^/v1/snapshots/([^/]+)$", path)
        if m:
            s = self.snaps.get(m[1])
            if not s or s["state"] == "deleted":
                return nf
            if method == "DELETE":
                s["state"] = "deleted"
                return 200, {}
            return 200, {"snapshot": s}
        m = re.match(r"^/v1/sandboxes/([^/]+)(/.*)?$", path)
        if not m:
            return nf
        sid, rest = m[1], m[2] or ""
        box = self.boxes.get(sid)
        if box is None or box["state"] == "deleted":
            return nf
        if rest == "":
            if method == "GET":
                return 200, {"sandbox": box}
            if method == "DELETE":
                box["state"] = "deleted"
                return 200, {}
            if method == "PATCH":
                box.update({k: v for k, v in b.items()})
                return 200, {"sandbox": box}
        if rest == "/sleep":
            box["state"] = "sleeping"
            return 200, {"sandbox": box}
        if rest == "/wake":
            box["state"] = "running"
            return 200, {"sandbox": box}
        if rest == "/labels":
            box["labels"] = dict(b["labels"])
            return 200, {"labels": box["labels"]}
        if rest == "/fork":
            out = []
            for _ in range(b["n"]):
                nid = self.add_box(template=box["template"])
                out.append(self.boxes[nid])
            return 201, {"sandboxes": out}
        if rest == "/snapshots" and method == "POST":
            snid = self._id("snap")
            self.snaps[snid] = {"id": snid, "name": (b or {}).get("name"), "state": "ready", "source_sandbox_id": sid,
                                "template": box["template"], "cpus": 1, "mem_mb": 1024}
            return 201, {"snapshot": self.snaps[snid]}
        if rest == "/exec" and method == "POST":
            if b.get("background"):
                bid = self._id("bg")
                code, out, _ = self._exec(b["cmd"])
                self.bg[bid] = {"id": bid, "state": "running", "exit": None, "output": None, "_polls": 0, "_final": (code, out)}
                return 202, {"exec": {"id": bid, "state": "running", "exit": None}}
            if b.get("stream"):
                return 200, self._stream(b["cmd"], b.get("env"))
            code, out, err = self._exec(b["cmd"], b.get("env"))
            return 200, {"exit": code, "stdout": out, "stderr": err}
        m2 = re.match(r"^/exec/([^/]+)(/stop)?$", rest)
        if m2:
            j = self.bg.get(m2[1])
            if j is None:
                return nf
            if m2[2]:
                j.update(state="exited", exit=143, output="", stopped=True)
                return 200, {"exec": {"id": j["id"], "state": "stopping"}}
            if j["state"] == "running" and self.bg_progress:
                j["output"] = self.bg_progress.pop(0)
                if not self.bg_progress:
                    j.update(state="exited", exit=j["_final"][0])
                return 200, {"exec": {k: v for k, v in j.items() if not k.startswith("_")}}
            if j["state"] == "running":
                j["_polls"] += 1
                if j["_polls"] >= self.bg_polls_until_exit:
                    j.update(state="exited", exit=j["_final"][0], output=j["_final"][1])
            return 200, {"exec": {k: v for k, v in j.items() if not k.startswith("_")}}
        if rest == "/files":
            p = q["path"]
            if method == "GET":
                if (sid, p) not in self.files:
                    return nf
                return 200, self.files[(sid, p)]
            if method == "PUT":
                self.files[(sid, p)] = b
                return 200, {"file": {"path": p, "size": len(b)}}
            if method == "DELETE":
                gone = [k for k in self.files if k[0] == sid and (k[1] == p or k[1].startswith(p + "/"))]
                dirs = {d for d in self.dirs if d[0] == sid and (d[1] == p or d[1].startswith(p + "/"))}
                if not gone and not dirs:
                    return nf
                for k in gone:
                    del self.files[k]
                self.dirs -= dirs
                return 200, {}
        if rest == "/files/list":
            p = q["path"].rstrip("/") or "/"
            names = {}
            for (s, fp), data in self.files.items():
                if s == sid and posixpath.dirname(fp) == p:
                    names[fp] = {"name": posixpath.basename(fp), "path": fp, "type": "file", "size": len(data)}
            for s, dp in self.dirs:
                if s == sid and posixpath.dirname(dp) == p:
                    names[dp] = {"name": posixpath.basename(dp), "path": dp, "type": "dir", "size": 0}
            if not names and (sid, p) not in self.dirs and p not in ("/workspace", "/home/user"):
                return nf
            return 200, {"path": p, "files": list(names.values())}
        if rest == "/files/mkdir":
            self.dirs.add((sid, b["path"]))
            return 201, {"file": {"path": b["path"], "type": "dir"}}
        if rest == "/files/link":
            return 201, {"link": {"url": f"https://h.example/dl?path={b['path']}&m={b.get('method', 'GET')}", "expires_at": 1}}
        if rest == "/previews":
            host = f"{b['port']}-{sid.lower().replace('_', '')}.kosa8labs.example"
            url = f"https://{host}/" if b.get("public") else f"https://p{b['port']}.example/?t=sig"
            return 201, {"preview": {"port": b["port"], "public": bool(b.get("public")), "host": host, "url": url}}
        if rest == "/egress" and method == "GET":
            b = box.get("egress") or {"internet": True}
            return 200, {"egress": {"internet": b.get("internet", not b.get("allow")), "allow": b.get("allow", []),
                                    "deny": b.get("deny", []), "presets": [], "source": "sandbox"}}
        if rest == "/egress" and method == "PUT":
            box["egress"] = dict(b)
            return 200, {"egress": {"internet": b.get("internet", not b.get("allow")), "allow": b.get("allow", []),
                                    "deny": b.get("deny", []), "presets": [], "source": "sandbox"}}
        if rest.startswith("/code") and not self.boxes.get(sid, {}).get("persistent"):
            # Like workers/cloud-api/src/code.js: the interpreter needs a persistent sandbox.
            return 409, {"error": "not_persistent", "message": "the code interpreter keeps state in a long-lived process, "
                         "which needs a persistent sandbox: create one with \"persistent\": true"}
        if rest == "/code":
            return 200, self.code_answer
        if rest == "/code/contexts" and method == "POST":
            return 201, {"context": {"name": "ctx-1", "execution_count": 0}}
        if rest == "/code/contexts" and method == "GET":
            return 200, {"contexts": [{"name": "default", "execution_count": 3}]}
        if re.match(r"^/code/contexts/[^/]+(/reset)?$", rest):
            return 200, {}
        return nf

    def _stream(self, argv, env=None):
        """A streamed exec's NDJSON: ``on_stream(argv, env)`` gives the frames
        (dicts; ``data`` as plain text, encoded here), else one stdout and one
        stderr frame from the buffered answer, a heartbeat and the exit."""
        frames = self.on_stream(argv, env) if self.on_stream is not None else None
        if frames is None:
            code, out, err = self._exec(argv, env)
            frames = [{"stream": "stdout", "data": "", "heartbeat": True}]
            if out:
                frames.append({"stream": "stdout", "data": out})
            if err:
                frames.append({"stream": "stderr", "data": err})
            frames.append({"done": True, "exit": code})
        lines = []
        for f in frames:
            f = dict(f)
            if "data" in f:
                d = f["data"]
                f["data"] = base64.b64encode(d if isinstance(d, bytes) else d.encode()).decode()
            lines.append(json.dumps(f).encode() + b"\n")
        return b"".join(lines)

    def _exec(self, argv, env=None):
        if self.on_exec is not None:
            r = self.on_exec(argv, env)
            if r is not None:
                return r
        return 0, f"ran {argv[-1]}\n", ""
