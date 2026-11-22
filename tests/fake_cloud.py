"""An in-memory kosa8 Cloud API for tests: an opener (sync or async) that
keeps sandboxes, labels, leases, files and RL runs, with hooks to inject
failures.

It mirrors the Worker on AzmxAI/kosa8 branch rl-integration
(workers/cloud-api/src/fanout.js, dataplane.js forkSandbox, failures.js,
idempotency.js, rl.js): the answer shapes of batch, fork, heartbeat, drain,
grade and /v1/rl/runs*, the all-or-nothing rules (a batch or fork that made
none answers its first refusal), the grade limits (64 files, 8 MiB decoded,
timeout_s 1-90), failure_class on every /v1/sandboxes error, and the
Idempotency-Key store (replay, 422 idempotency_mismatch, a 5xx or 402/423/429
not kept)."""

import base64
import binascii
import hashlib
import itertools
import json
import re
import threading
import time
import urllib.parse

MAX_BATCH = 1000
MAX_FORK = 100
MAX_FORK_TOTAL = 1000
DRAIN_MAX = 1000
MIN_LEASE_S, MAX_LEASE_S = 60, 86400
MAX_LABELS = 16
GRADE_MAX_FILES = 64
GRADE_MAX_BYTES = 8 * 1024 * 1024
GRADE_TAIL = 64 * 1024
MAX_ROLLOUTS_PER_CALL = 500
META_MAX = 8192
STDOUT_TAIL = 4096
FAILURE_CLASSES = ["ok", "exit_nonzero", "timeout", "infra", "quota", "client"]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
KEY_RE = re.compile(r"^[\x21-\x7e]{1,255}$")
IDEM_ROUTES = [re.compile(r"^/v1/sandboxes$"), re.compile(r"^/v1/sandboxes/batch$"),
               re.compile(r"^/v1/sandboxes/[A-Za-z0-9_]{1,64}/(fork|grade)$")]

INFRA_CODES = {
    "host_failed", "not_ready", "no_capacity", "host_unreachable", "host_unavailable", "host_busy", "host_restarting",
    "wake_failed", "try_again", "internal", "kernel_failed", "egress_failed", "dns_unreachable", "host_lost",
    "host_removed", "insufficient_storage", "starting",
}
RATE_CODES = {"rate_limited", "slow_down", "too_soon"}
QUOTA_CODES = {
    "quota_exceeded", "running_limit", "plan_required", "plan_limit", "plan_feature", "payment_required",
    "payment_declined", "spending_cap_reached", "snapshot_limit", "org_frozen", "org_suspended", "too_many_runs",
}


def failure_of(status, code=None):
    """failures.js failureOf."""
    c = code if isinstance(code, str) else ""
    if status < 400:
        return {"failure_class": "ok", "retryable": False}
    if c == "exec_timeout":
        return {"failure_class": "timeout", "retryable": False}
    if "timeout" in c:
        return {"failure_class": "timeout", "retryable": True}
    if c in RATE_CODES:
        return {"failure_class": "quota", "retryable": True}
    if c in QUOTA_CODES or status in (402, 423):
        return {"failure_class": "quota", "retryable": False}
    if status == 429:
        return {"failure_class": "quota", "retryable": c != "quota_exceeded"}
    if c in ("not_available", "not_supported") or status == 501:
        return {"failure_class": "infra", "retryable": False}
    if c in INFRA_CODES or status >= 500:
        return {"failure_class": "infra", "retryable": True}
    return {"failure_class": "client", "retryable": False}


def failed_entry(index, status, body):
    """failures.js failedEntry."""
    body = body if isinstance(body, dict) else {}
    code = body["error"] if isinstance(body.get("error"), str) else f"http_{status}"
    f = failure_of(status, code)
    return {"index": index, "code": code,
            "message": body["message"][:500] if isinstance(body.get("message"), str) else f"HTTP {status}",
            "failure_class": body.get("failure_class", f["failure_class"]),
            "retryable": body["retryable"] if isinstance(body.get("retryable"), bool) else f["retryable"]}


def parse_reward(stdout, exit_code):
    """fanout.js parseReward."""
    for line in reversed((stdout or "").strip().split("\n")):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict) and isinstance(v.get("reward"), (int, float)) and not isinstance(v["reward"], bool):
            m = v.get("metrics")
            return v["reward"], (m if isinstance(m, dict) else None), "json"
    if not isinstance(exit_code, int):
        return None, None, None
    return (1 if exit_code == 0 else 0), None, "exit"


class Refusal(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.answer = (status, {"error": code, "message": message, **extra})


def err(status, code, message, **extra):
    return status, {"error": code, "message": message, **extra}


class FakeCloud:
    def __init__(self):
        self.lock = threading.RLock()
        self.ids = itertools.count(1)
        self.sandboxes = {}  # id -> dict(info..., files={}, labels={})
        self.deleted = []
        self.calls = []  # (method, path, body)
        self.headers = []  # (method, path, headers) of every call
        self.runs = {}
        self.rollouts = []
        self.heartbeats = []
        self.drains = []
        # Idempotency-Key store: key -> {"hash", "status", "body"}
        self.idem = {}
        self.replays = 0
        #: Keys whose next use answers 409 idempotency_in_progress (as if
        #: the first request were still running), once each.
        self.in_progress = set()
        #: lose_answer(method, path) -> True: the request runs (and its answer
        #: is kept) but the caller gets no answer, as a dropped connection.
        self.lose_answer = None
        # Hooks: return a (status, body[, headers]) to answer instead, or None.
        # The answer is the handler's: it goes through the Idempotency-Key store.
        self.before = None
        # exec(sandbox, argv, env) -> dict answer (exit/stdout/stderr)
        self.on_exec = lambda sb, argv, env: {"exit": 0, "stdout": "ok\n", "stderr": ""}
        # grade(sandbox, body, files) -> the grader exec's answer (exit, stdout,
        # stderr), a full grade answer (with "reward"), or a (status, body) tuple.
        self.on_grade = None
        self.create_state = "running"
        self.running_limit = None
        self.drain_max = DRAIN_MAX

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

    def _room(self):
        if self.running_limit is None:
            return 10 ** 9
        return max(0, self.running_limit - len(self.sandboxes))

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
        key = next((v for k, v in headers.items() if k.lower() == "idempotency-key"), None)
        with self.lock:
            self.calls.append((method, path, parsed))
            self.headers.append((method, path, dict(headers)))
        keyed = key is not None and method == "POST" and any(r.match(path) for r in IDEM_ROUTES)
        if keyed:
            out = self._idempotent(key, method, path, body, lambda: self._handle(method, path, query, parsed))
        else:
            out = self._handle(method, path, query, parsed)
        if self.lose_answer is not None and self.lose_answer(method, path):
            raise ConnectionError("the connection dropped before the answer")
        return self._encode(*out)

    async def aopen(self, method, url, headers, body, timeout):
        return self(method, url, headers, body, timeout)

    def _handle(self, method, path, query, parsed):
        if self.before is not None:
            hooked = self.before(method, path, parsed)
            if hooked is not None:
                return self._classed(path, hooked)
        with self.lock:
            try:
                out = self._route(method, path, query, parsed)
            except Refusal as e:
                out = e.answer
        return self._classed(path, out)

    @staticmethod
    def _classed(path, out):
        """failures.js withFailureClass, applied to /v1/sandboxes answers."""
        status, payload = out[0], out[1]
        if (status >= 400 and path.startswith("/v1/sandboxes") and isinstance(payload, dict)
                and "failure_class" not in payload):
            payload = {**payload, **failure_of(status, payload.get("error"))}
            return (status, payload) + tuple(out[2:])
        return out

    def _idempotent(self, key, method, path, body, run):
        """idempotency.js withIdempotency (one request at a time here)."""
        if not KEY_RE.match(key):
            return err(400, "invalid_idempotency_key", "Idempotency-Key must be 1-255 printable ASCII characters")
        h = hashlib.sha256(f"{method} {path}\n".encode() + (body or b"")).hexdigest()
        with self.lock:
            row = self.idem.get(key)
            if row is not None:
                if row["hash"] != h:
                    return err(422, "idempotency_mismatch",
                               "this Idempotency-Key was used in the last 24 hours with another request",
                               failure_class="client", retryable=False)
                if key in self.in_progress:
                    self.in_progress.discard(key)
                    return err(409, "idempotency_in_progress", "a request with this Idempotency-Key is still running",
                               retry_after_s=2, failure_class="client", retryable=False)
                self.replays += 1
                return (row["status"], json.loads(row["body"]), {"Idempotent-Replayed": "true"})
            self.idem[key] = {"hash": h, "status": None, "body": None}
        out = run()
        status = out[0]
        settled = status < 500 and status not in (402, 423, 429) and not isinstance(out[1], bytes)
        with self.lock:
            if settled:
                self.idem[key].update(status=status, body=json.dumps(out[1]))
            else:
                self.idem.pop(key, None)
        return out

    @staticmethod
    def _encode(status, payload, headers=None):
        if isinstance(payload, bytes):
            data = payload
        else:
            data = b"" if payload is None else json.dumps(payload).encode()
        return (status, data, headers or {})

    # -- /v1/sandboxes ------------------------------------------------------

    def _fanout(self, b):
        """fanout.js parseFanout: run_id, labels, lease_s, prefix, partial_ok."""
        if not isinstance(b, dict):
            raise Refusal(400, "invalid_json", "the body must be a JSON object")
        if "run_id" in b and (not isinstance(b["run_id"], str) or not RUN_ID_RE.match(b["run_id"])):
            raise Refusal(400, "invalid_run_id", "run_id must be 1-128 letters, digits, '_', '.', ':' or '-'")
        labels = dict(b.get("labels") or {})
        if b.get("run_id"):
            labels["kosa8.run"] = b["run_id"]
        if len(labels) > MAX_LABELS:
            raise Refusal(400, "invalid_labels", f"a sandbox has at most {MAX_LABELS} labels, kosa8.run included")
        if "lease_s" in b:
            v = b["lease_s"]
            if not isinstance(v, int) or isinstance(v, bool) or not MIN_LEASE_S <= v <= MAX_LEASE_S:
                raise Refusal(400, "invalid_lease",
                              f"lease_s must be a whole number of seconds from {MIN_LEASE_S} to {MAX_LEASE_S}")
            if "ttl_s" in b or "delete_after" in b:
                raise Refusal(400, "invalid_lease", "give lease_s or ttl_s/delete_after, not both")
        if "partial_ok" in b and not isinstance(b["partial_ok"], bool):
            raise Refusal(400, "invalid_partial_ok", "partial_ok must be true or false")
        any_ = any(k in b for k in ("run_id", "labels", "lease_s", "prefix", "partial_ok"))
        return labels, b.get("lease_s"), b.get("partial_ok"), any_

    def _none_made(self, first, failed, requested, made):
        """The all-or-nothing answer: the made ones deleted, the first refusal answered."""
        for s in made:
            self.sandboxes.pop(s["id"], None)
        status, body = first
        extra = {"requested": requested, "created": 0, "failed": failed[:100]}
        if made:
            extra["deleted"] = [s["id"] for s in made]
        body = dict(body)
        f = failure_of(status, body.get("error"))
        body.setdefault("failure_class", f["failure_class"])
        body.setdefault("retryable", f["retryable"])
        return status, {**body, **extra}

    def _route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if parts[:2] == ["v1", "rl"]:
            return self._rl(method, parts[2:], query, body)
        if parts[:2] != ["v1", "sandboxes"]:
            return err(404, "not_found", path)
        rest = parts[2:]
        if not rest:
            if method == "POST":
                b = body or {}
                if self._room() <= 0:
                    return err(429, "quota_exceeded", "at the running limit")
                if "lease_s" in b:
                    self._fanout({"lease_s": b["lease_s"]})
                info = {k: v for k, v in b.items() if k in ("template", "image", "lease_s", "snapshot", "name")}
                sb = self._new(**info)
                if b.get("labels"):
                    sb["labels"] = dict(b["labels"])
                return 201, {"sandbox": self._public(sb)}
            if method == "GET":
                sel = query.get("label")
                want = dict([sel.split("=", 1)]) if sel and "=" in sel else {}
                return 200, {"sandboxes": [self._public(s) for s in self.sandboxes.values()
                                           if all(s["labels"].get(k) == v for k, v in want.items())]}
        if rest == ["batch"]:
            return self._batch(body or {})
        if rest == ["heartbeat"]:
            self.heartbeats.append(body)
            sel = self._selector(body)
            if "extend_s" in sel:
                v = sel["extend_s"]
                if not isinstance(v, int) or not MIN_LEASE_S <= v <= MAX_LEASE_S:
                    return err(400, "invalid_extend", "extend_s must be a whole number of seconds from 60 to 86400")
            hits = [s for s in self._match(sel) if s.get("lease_s")]
            return 200, {"extended": len(hits), "at": int(time.time() * 1000)}
        if rest == ["drain"]:
            self.drains.append(body)
            hits = self._match(self._selector(body))
            more = len(hits) > self.drain_max
            hits = hits[: self.drain_max]
            for s in hits:
                self.sandboxes.pop(s["id"], None)
                self.deleted.append(s["id"])
            return 200, {"deleted": len(hits), "ids": [s["id"] for s in hits], "failed": [], "more": more}
        sid = rest[0]
        sb = self.sandboxes.get(sid)
        if sb is None:
            return err(404, "not_found", f"no sandbox {sid}")
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
            ans = dict(self.on_exec(sb, body["cmd"], env))
            ex = ans.get("exit")
            ans.setdefault("failure_class",
                           "infra" if not isinstance(ex, int) else ("ok" if ex == 0 else "exit_nonzero"))
            return 200, ans
        if sub == ["files"]:
            p = query["path"]
            if method == "PUT":
                sb["files"][p] = body
                return 200, {"file": {"path": p, "size": len(body)}}
            if method == "GET":
                if p not in sb["files"]:
                    return err(404, "not_found", p)
                return 200, sb["files"][p]
            if method == "DELETE":
                sb["files"].pop(p, None)
                return 200, {}
        if sub == ["files", "list"]:
            return 200, {"files": [{"path": k} for k in sb["files"]]}
        if sub == ["fork"]:
            return self._fork(sb, body or {})
        if sub == ["grade"]:
            return self._grade(sb, body or {})
        if sub == ["snapshots"]:
            return 201, {"snapshot": {"id": f"snap_{next(self.ids)}", "name": (body or {}).get("name")}}
        return err(404, "not_found", path)

    @staticmethod
    def _selector(body):
        """fanout.js selector: run_id, labels or ids."""
        b = body or {}
        if not (b.get("run_id") or b.get("labels") or b.get("ids")):
            raise Refusal(400, "invalid_selector", "give run_id, labels or ids: which sandboxes")
        if "ids" in b and (not isinstance(b["ids"], list) or not 1 <= len(b["ids"]) <= DRAIN_MAX):
            raise Refusal(400, "invalid_ids", f"ids must be a list of 1 to {DRAIN_MAX} sandbox ids")
        return b

    def _batch(self, b):
        """fanout.js batchCreate."""
        count = b.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_BATCH:
            return err(400, "invalid_count", f"count must be a whole number from 1 to {MAX_BATCH}")
        if "name" in b:
            return err(400, "invalid_name", "a batch names its sandboxes with prefix, not name")
        labels, lease_s, partial_ok, _ = self._fanout(b)
        partial_ok = True if partial_ok is None else partial_ok
        room = min(count, self._room())
        if room < count and (not partial_ok or room == 0):
            return err(429, "quota_exceeded", f"a batch of {count} would take this organization past its limit "
                       f"({room} more fit now)", requested=count, created=0, failure_class="quota", retryable=False)
        made, failed = [], []
        for i in range(count):
            if i >= room:
                failed.append({"index": i, "code": "running_limit", "message": f"past the organization's running "
                               f"limit: {room} of {count} fit", "failure_class": "quota", "retryable": False})
                continue
            sb = self._new(lease_s=lease_s, template=b.get("template"))
            sb["labels"] = dict(labels)
            if b.get("prefix"):
                sb["name"] = f"{b['prefix']}-{i + 1:04d}"
            made.append(self._public(sb))
        if failed and (not partial_ok or not made):
            return self._none_made((429, {"error": "running_limit", "message": failed[0]["message"]}),
                                   failed, count, made)
        out = {"sandboxes": made, "failed": failed, "requested": count, "created": len(made)}
        if b.get("run_id"):
            out["run_id"] = b["run_id"]
        return 200, out

    def _fork(self, sb, b):
        """dataplane.js forkSandbox (with the fan-out fields)."""
        n = b.get("n", 1)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_FORK_TOTAL:
            return err(400, "invalid_n", f"n must be a whole number from 1 to {MAX_FORK_TOTAL}")
        labels, lease_s, partial_ok, any_ = self._fanout(b)
        partial_ok = (any_ or n > MAX_FORK) if partial_ok is None else partial_ok
        if sb["state"] not in ("running", "sleeping"):
            return err(409, "not_running",
                       f"the sandbox is {sb['state']}; only a running or sleeping sandbox can be forked")
        made, failed, first = [], [], None
        for i in range(n):
            if self._room() <= 0:
                first = (429, {"error": "quota_exceeded",
                               "message": f"the organization is at its limit: {i} of {n} copies were made"})
                failed.extend(failed_entry(j, 429, first[1]) for j in range(i, n))
                break
            k = self._new(parent_id=sb["id"], lease_s=lease_s or sb.get("lease_s"))
            k["files"] = dict(sb["files"])
            k["labels"] = dict(labels)
            made.append(self._public(k))
        if not made or (not partial_ok and failed):
            if not any_ and n <= MAX_FORK:  # the original answer, unchanged
                for s in made:
                    self.sandboxes.pop(s["id"], None)
                return first
            return self._none_made(first, failed, n, made)
        out = {"parent": sb["id"], "sandboxes": made, "host_ms": 3, "requested": n, "created": len(made),
               "failed": failed}
        if b.get("run_id"):
            out["run_id"] = b["run_id"]
        return 201, out

    def _grade(self, sb, b):
        """fanout.js parseGrade + gradeSandbox."""
        cmd = b.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            return err(400, "invalid_cmd", "cmd must be a non-empty list of strings")
        t = b.get("timeout_s")
        if t is not None and (not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 90):
            return err(400, "invalid_timeout", "timeout_s must be a whole number of seconds from 1 to 90")
        files, total = {}, 0
        if "files" in b:
            if not isinstance(b["files"], dict):
                return err(400, "invalid_files", "files must be an object")
            if len(b["files"]) > GRADE_MAX_FILES:
                return err(400, "invalid_files", f"at most {GRADE_MAX_FILES} files")
            for p, v in b["files"].items():
                if not p.startswith("/") or ".." in p.split("/"):
                    return err(400, "invalid_path", f"file path {p!r} must be absolute, without ..")
                try:
                    data = base64.b64decode(v, validate=True)
                except (binascii.Error, ValueError):
                    return err(400, "invalid_files", f"file {p} must be base64")
                total += len(data)
                if total > GRADE_MAX_BYTES:
                    return err(413, "too_large", "files add up to more than 8 MiB")
                files[p] = data
        ro = b.get("rollout")
        if ro is not None:
            if not isinstance(ro, dict):
                return err(400, "invalid_rollout", "rollout must be an object {step?, group?, index?, meta?}")
            if "group" in ro and (not isinstance(ro["group"], str) or len(ro["group"]) > 200):
                return err(400, "invalid_rollout", "rollout.group must be a string of at most 200 characters")
            for k in ("step", "index"):
                if k in ro and (not isinstance(ro[k], int) or isinstance(ro[k], bool)):
                    return err(400, "invalid_rollout", f"rollout.{k} must be a whole number")
            if "meta" in ro and len(json.dumps(ro["meta"])) > 16 * 1024:
                return err(400, "invalid_rollout", "rollout.meta must be an object of at most 16 KiB as JSON")
        if "run_id" in b and (not isinstance(b["run_id"], str) or not RUN_ID_RE.match(b["run_id"])):
            return err(400, "invalid_run_id", "run_id must be 1-128 letters, digits, '_', '.', ':' or '-'")
        keep = b.get("keep") is True
        grader = self._new(parent_id=sb["id"], lease_s=600)
        grader["files"] = {**sb["files"], **files}
        ans = self.on_grade(sb, b, files) if self.on_grade is not None else None
        if isinstance(ans, tuple):  # the grade's own error answer (a fork that failed, ...)
            self.sandboxes.pop(grader["id"], None)
            return ans
        if ans is None:
            ans = {"exit": 0, "stdout": '{"reward": 1.0}\n', "stderr": ""}
        if "reward" in ans:  # a full answer, as the test wrote it
            result = {"metrics": None, "reward_from": "json", "stdout": "", "stderr": "", "ms": 5,
                      "kept": keep, "sandbox_id": sb["id"], **ans}
            result.setdefault("exit", 0)
            fc = result.setdefault("failure_class", "ok" if result["exit"] == 0 else "exit_nonzero")
            result.setdefault("retryable", fc == "infra")
        else:  # the grader exec's answer
            exit_code = ans.get("exit")
            if ans.get("error"):  # the command did not finish: no reward
                exit_code = None
                f = failure_of(ans.get("status", 502), ans["error"])
                if f["failure_class"] == "client":
                    f = {"failure_class": "infra", "retryable": False}
                reward, metrics, rfrom = None, None, None
            else:
                reward, metrics, rfrom = parse_reward(ans.get("stdout"), exit_code)
                f = {"failure_class": "infra", "retryable": True} if not isinstance(exit_code, int) else \
                    {"failure_class": "ok" if exit_code == 0 else "exit_nonzero", "retryable": False}
            result = {"reward": reward, "metrics": metrics, "reward_from": rfrom, "exit": exit_code,
                      "stdout": (ans.get("stdout") or "")[-GRADE_TAIL:],
                      "stderr": (ans.get("stderr") or "")[-GRADE_TAIL:],
                      **f, "ms": 5, "kept": keep, "sandbox_id": sb["id"]}
            if ans.get("error"):
                result.update(error=ans["error"], message=ans.get("message"))
        result.setdefault("grader_id", grader["id"])
        if not keep:
            self.sandboxes.pop(grader["id"], None)
        if b.get("run_id"):
            ro = dict(ro or {})
            meta = ro.get("meta")
            rid = b["run_id"]
            if rid in self.runs and (meta is None or len(json.dumps(meta, separators=(",", ":"))) <= META_MAX):
                self.rollouts.append({"run_id": rid, "sandbox_id": sb["id"], "grader_id": result["grader_id"],
                                      "reward": result["reward"], "failure_class": result["failure_class"],
                                      "exit": result["exit"], "from": "grade", "source": "grade", **ro})
                recorded = True
            else:
                recorded = False
            result["run_id"] = rid
            result["recorded"] = recorded
        return 200, result

    # -- /v1/rl/runs ----------------------------------------------------------

    def _rl(self, method, parts, query, body):
        if parts == ["runs"]:
            if method == "POST":
                if not isinstance((body or {}).get("name"), str):
                    return err(400, "invalid_name", "name is required")
                rid = f"run_{next(self.ids)}"
                self.runs[rid] = {"id": rid, "name": body["name"], "project": body.get("project"),
                                  "status": "running", "config": body.get("config"),
                                  "label": {"kosa8.run": rid}, "finished_at": None}
                return 201, {"run": self.runs[rid]}
            runs = [r for r in self.runs.values()
                    if all(r.get(k) == query[k] for k in ("project", "status", "name") if k in query)]
            return 200, {"runs": [{**r, "summary": {"rollouts": self._count(r["id"])}} for r in runs],
                         "next_cursor": None}
        rid = parts[1]
        run = self.runs.get(rid)
        if run is None:
            return err(404, "not_found", "no such run")
        if parts[2:] == []:
            if method == "PATCH":
                if "status" in body and body["status"] not in ("running", "finished", "failed"):
                    return err(400, "invalid_status", "status must be one of running, finished, failed")
                run.update(body)
                return 200, {"run": run}
            return 200, {"run": run, "summary": {"rollouts": self._count(rid)}, "recommendations": []}
        if parts[2:] == ["rollouts"]:
            if method == "POST":
                rows = (body or {}).get("rollouts")
                if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROLLOUTS_PER_CALL:
                    return err(400, "invalid_rollouts", f"rollouts must be a list of 1 to {MAX_ROLLOUTS_PER_CALL}")
                for i, r in enumerate(rows):
                    fc = r.get("failure_class")
                    if fc is not None and fc not in FAILURE_CLASSES:
                        return err(400, "invalid_rollout", f"rollouts[{i}].failure_class must be one of "
                                   + ", ".join(FAILURE_CLASSES))
                    if r.get("meta") is not None and len(json.dumps(r["meta"], separators=(",", ":"))) > META_MAX:
                        return err(400, "invalid_rollout", f"rollouts[{i}].meta is over {META_MAX} bytes as JSON")
                    if isinstance(r.get("stdout_tail"), str) and len(r["stdout_tail"]) > STDOUT_TAIL:
                        rows[i] = {**r, "stdout_tail": r["stdout_tail"][-STDOUT_TAIL:]}
                ids = []
                for r in rows:
                    ids.append(f"ro_{next(self.ids)}")
                    self.rollouts.append({"run_id": rid, "from": "sdk", "source": "api", "id": ids[-1], **r})
                return 200, {"recorded": len(rows), "ids": ids}
            rows = [r for r in self.rollouts if r["run_id"] == rid]
            return 200, {"rollouts": rows, "next_cursor": None, "query": query}
        if parts[2:] == ["export"]:
            rows = [r for r in self.rollouts if r["run_id"] == rid]
            return 200, ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        return err(404, "not_found", "/".join(parts))

    def _count(self, rid):
        return len([r for r in self.rollouts if r["run_id"] == rid])
