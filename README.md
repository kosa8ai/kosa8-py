# kosa8 for Python

Python client for [kosa8](https://kosa8.com): microVM sandboxes for AI agents, with
checkpoint, rewind, fork and explore. The counterpart of
[`@kosa8/sdk`](https://www.npmjs.com/package/@kosa8/sdk), method for method.

No dependencies. It talks to **kosa8 Cloud** (`https://api.kosa8.com`) with an API
key, or to the local kosa8 daemon over its unix socket, or to a remote kosa8 worker.

```sh
pip install kosa8
```

## Quickstart: kosa8 Cloud

Make an API key in the dashboard (https://app.kosa8.com) and set `KOSA8_API_KEY`.

```python
from kosa8 import Kosa8Cloud

cloud = Kosa8Cloud()  # KOSA8_API_KEY from the environment; or Kosa8Cloud("k8c_live_...")

box = cloud.sandboxes.create(template="python", ttl_s=3600)   # deleted automatically after an hour
r = box.exec(["python3", "-c", "print(6 * 7)"])
print(r.exit_code, r.stdout)          # 0 42

box.exec("echo hello > /workspace/hello.txt")   # /workspace is kept between commands
print(box.exec("cat /workspace/hello.txt").stdout)

small = cloud.sandboxes.create(size="0.25x0.5", ttl_s=600)   # 0.25 vCPU, 512 MB (or cpus=0.25, mem_mb=512)
small.delete()

box.sleep()                           # costs only snapshot storage while asleep
box.exec("ls /workspace")             # wakes it, files and all
box.delete()
```

`cloud.templates()` lists the templates your org can start. Without `template=`
you get the org's default, `base` (see below).

### More of the Cloud API

```python
box.update(ttl_s=7200)                 # PATCH: a new lifetime from now
box.update(clear_lifetime=True)        # no automatic delete
box.update(sleep_after_s=0)            # never sleep when idle

for b in cloud.sandboxes.iter():       # every sandbox, page by page
    print(b.id, b.state, b.info.get("delete_after"))
page = cloud.sandboxes.list_page(limit=50)   # one page; page.next_cursor is None at the end

# These need the host's data plane. On a host without it they raise
# Kosa8UnsupportedError (501 not_available / not_supported): not every
# kosa8 Cloud host has it yet.
box.files.write("/workspace/app.py", "print('hi')\n")
print(box.files.read_text("/workspace/app.py"))
print([f["name"] for f in box.files.list("/workspace")])
box.files.mkdir("/workspace/data")
box.files.delete("/workspace/data", recursive=True)

ckpt = box.checkpoint("before the risky bit")   # memory, processes and disk
box.exec("rm -rf /workspace/*")
box.rewind(ckpt["id"])                          # back; the state before is kept as an automatic backup
forks = box.fork(4)                             # four live copies
print(box.ports())                              # listening TCP ports

cloud.functions.deploy("hello", {
    "runtime": "python3.12",
    "code": 'def handler(event):\n    return {"hello": event.get("name", "world")}\n',
})
out = cloud.functions.invoke("hello", {"name": "kosa8"})
print(out["ok"], out["result"])  # True {'hello': 'kosa8'}
```

### Sized and Docker sandboxes, env, background commands, snapshots

```python
# 4 vCPU / 8 GiB, Ubuntu with common toolchains, its own Docker engine and env.
box = cloud.sandboxes.create(
    size="4x8",                                       # 0.25x0.5, 0.5x1, 1x2, 2x4, 4x8, 8x16 or 16x32 (CLOUD_SIZES)
    image="kosa8-template/universal-24.04-20261115.2",
    docker=True,                                      # implies persistent
    env={"NODE_ENV": "production"},                   # every command gets it; never logged
    name="web-1", labels={"team": "web"}, ttl_s=3600,
)                                                     # a cold boot is waited for (wait=False to skip)

job = box.exec_background("dockerd > /var/log/dockerd.log 2>&1 & sleep 3; docker run --rm hello-world")
done = job.wait()                                     # or job.status() to poll yourself
print(done["exit"], done["output"])
long = box.exec_background("make test", timeout_s=3600)   # the host ends it after an hour
long.stop()                                           # SIGTERM, then SIGKILL 5 s later

snap = box.snapshot("web-ready")                      # durable: memory, processes and disk
copy = cloud.sandboxes.create(snapshot=snap["id"], ttl_s=600)
cloud.snapshots.list(); cloud.snapshots.delete(snap["id"])
cloud.sandboxes.list(name="web-1", labels={"team": "web"})
```

`cpu="dedicated"` gives whole physical cores; `disk_gb` is 8 to 200; `region` takes `us-ash1`, `eu-hel1`, `us` or `eu`; `egress={...}` and `secrets=[...]` set its network policy and the secrets it holds at boot. A create from a `snapshot` takes the snapshot's template, size and image, so those options are refused before any request.

### Previews, terminal, code interpreter, egress, schedules

```python
box.exec_background("python3 -m http.server 3000")
print(box.previews.create(3000)["url"])               # private signed link on kosa8labs.com
box.previews.create(8080, public=True, wake=True)
box.previews.list(); box.previews.link(3000, expires_in_s=600); box.previews.rotate(3000); box.previews.delete(3000)

print(box.terminal(cols=120, rows=32)["url"])         # wss:// URL for a PTY WebSocket
nb = cloud.sandboxes.create(template="python", persistent=True)   # the code interpreter needs a persistent sandbox
r = nb.run_code("import math; math.pi")               # stateful Python; charts come back as image/png
nb.code_contexts.create("nb"); nb.code_contexts.reset("nb")

box.set_egress(internet=False, presets=["pypi"], allow=["api.example.com"])
box.reset_egress(); cloud.egress_presets(); cloud.set_org_egress({"internet": False}, enforce=True)

box.schedules.create("0 * * * *", "date >> /workspace/hourly.log", wake=True)
box.files.write_raw("/workspace/big.bin", data)       # no 8 MiB cap; read_raw too
box.files.link("/workspace/big.bin")                  # presigned URL for curl, 10-300 s
box.metrics(); box.logs(limit=20); box.secrets(); box.set_labels({"team": "ml"})
cloud.capabilities()                                  # feature detection
```

### Secrets and Pipelines

```python
# The value goes in one request body and never comes back; sandboxes hold a placeholder.
cloud.secrets.set("OPENAI", os.environ["OPENAI_API_KEY"], upstream="api.openai.com", pipelines=True)
box = cloud.sandboxes.create(secrets=["OPENAI"])
cloud.secrets.list(); cloud.secrets.delete("OPENAI")

run = cloud.pipelines.run("https://github.com/me/app", ref="main", size="4x8", secrets=["NPM_TOKEN"])
done = cloud.pipelines.wait(run["id"])                # success, failure, cancelled or error
print(cloud.pipelines.logs(run["id"])["logs"][0]["text"])
for a in cloud.pipelines.artifacts(run["id"]):
    tgz = cloud.pipelines.download_artifact(run["id"], a["job"], a["idx"])
hook = cloud.pipelines.hooks.create("https://github.com/me/app", provider="github", cron="0 3 * * *")
print(hook["url"], hook["secret"])                    # give both to the repository's webhook settings
```

### Browsers (headless Chromium over CDP)

A cloud browser is headless Chromium you drive over CDP with Playwright or
Puppeteer, or use through quick page loads. $0.12 per browser-hour, billed per second;
`ttl_s` (30 to 3600, default 300) closes it on its own.

```python
with cloud.browser(ttl_s=300) as b:                   # closed on the way out
    png = b.screenshot("https://example.com")         # PNG bytes (full_page=True for the whole page)
    page = b.goto("https://example.com")              # url, title, text

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(b.cdp_url)   # cdp_url is valid 10 minutes; b.refresh() for a new one
        print(browser.contexts[0].pages[0].title() if browser.contexts and browser.contexts[0].pages else "")

cloud.list_browsers(state="all"); cloud.get_browser("brw_..."); cloud.browsers.delete("brw_...")
cloud.browsers.cdp_url("brw_...")   # a fresh wss://.../v1/browsers/{id}/cdp?token=... for an open browser
```

`AsyncCloud` has the same: `async with await cloud.browser() as b: await b.goto(url)`.

### AI gateway

Model calls from sandboxes go through the org's gateway: provider keys stay on
kosa8 Cloud, with caching, a rate limit and per-sandbox usage.

```python
cloud.set_ai_key("openai", os.environ["OPENAI_API_KEY"])   # never sent back; list_ai_keys() shows a hint
cloud.update_ai_settings(enabled=True, cache_ttl_s=3600, rate_per_min=120)
box = cloud.sandboxes.create(ai_gateway=True)   # OPENAI_BASE_URL, ANTHROPIC_BASE_URL, KOSA8_AI_GATEWAY_URL, KOSA8_AI_TOKEN set
print(cloud.ai_usage("2026-11")["total"])       # calls, cost_usd, tokens_in, tokens_out, cached, errors
cloud.delete_ai_key("openai")

# Outside a sandbox: point the provider's SDK at the gateway, with your kosa8 key as its key.
from openai import OpenAI
llm = OpenAI(base_url=cloud.ai.gateway_url("openai"), api_key=os.environ["KOSA8_API_KEY"])   # .../v1/ai/gw/openai/
```

### Account, keys, webhooks, audit, benchmarks, RL

Each is on `Kosa8Cloud` and, awaited, on `AsyncCloud`.

```python
cloud.pricing()["rates"]; cloud.limits(); cloud.usage_insights()
cloud.images(); cloud.lineage(); cloud.checkpoints()        # every sandbox's checkpoints
cloud.request_limit(100, reason="RL sweep")                 # ask for a higher running limit

k = cloud.api_keys.create("ci", scopes=["sandboxes:read", "sandboxes:write"])   # k["api_key"]["secret"], once
cloud.api_keys.rotate(k["api_key"]["id"], grace_s=3600); cloud.api_keys.revoke(k["api_key"]["id"])

wh = cloud.webhooks.create("https://me.example/kosa8", events=["sandbox.created"])   # wh["secret"], once
cloud.webhooks.test(wh["id"]); cloud.webhooks.deliveries(wh["id"]); cloud.webhooks.delete(wh["id"])

page = cloud.audit.list(limit=50)            # page["events"], page["next_before_seq"]
cloud.audit.verify()["ok"]; cloud.audit.export()

cloud.benchmarks.list(); cloud.benchmarks.instances("swe-bench-verified", limit=10)
cloud.benchmarks.prepare("swe-bench-verified", 20, seed=1)   # build 20 environments ahead of use

cloud.rl.compare(["run_a", "run_b"]); cloud.rl.alerts()
cloud.rl.create_alert("infra_rate", 0.2, run_id="run_a")
cloud.functions.invocations("hello", limit=20)
box.previews.delete(3000); box.schedules.delete("sch_..."); box.code_contexts.delete("default")
```

### How Cloud sandboxes run commands

- The `base` template is **Alpine Linux**: `sh` is busybox, and there is no `bash`,
  `python` or `git`. Use `template="python"` (or another key from `cloud.templates()`)
  for Python, or pass `image=` to `exec`.
- **Each exec is a fresh process; files in `/workspace` persist for the sandbox's
  life; installed packages need `persistent=True`.** A command may run in
  a fresh container, so `cd`, `export`, processes it leaves running and anything
  written or installed outside `/workspace` (`apk add`, `pip install` into the
  system) may be gone by the next command. Create with `persistent=True` (one
  long-lived container) to keep the whole filesystem and background processes;
  the code interpreter (`run_code`) needs it, and it counts against your plan's
  persistent-sandbox limit. Install into `/workspace`, for
  example `pip install --target /workspace/lib ...` with `PYTHONPATH=/workspace/lib`,
  or chain the steps in one `sh -c`.
- **A buffered `exec` runs at most 90 seconds** and raises
  `Kosa8ExecTimeoutError`. `exec(cmd, timeout_s=30)` stops a command sooner, in
  the sandbox (1 to 90 whole seconds). A **streamed exec** has no such limit and
  hands over output as it prints: `box.exec(cmd, on_stdout=print, on_stderr=print)`,
  `box.exec(cmd, timeout_s=600)` (a `timeout_s` over 90 streams), or
  `for stream, text in box.exec_stream(cmd): ...` (`timeout_s` up to a day). Or run
  long work in the background and poll it:
  `box.exec("nohup sh -c 'make test > /workspace/test.log 2>&1; echo $? > /workspace/test.exit' &")`.
- A request body (command and env) is at most 64 KiB; a file read or write is at
  most 8 MiB.
- Set `ttl_s` (60 s to 90 days) or `delete_after` on create so a crashed script
  cannot leave a sandbox running.
- Labels (up to 16) go on with `labels=` at create or `box.set_labels()`;
  `list(labels=...)`, `heartbeat` and `drain` select by them.

- **Overflow capacity.** When kosa8's own hosts are full, a plain sandbox may run
  on overflow capacity (Cloudflare; `box.overflow` is `True`, `region` is
  `cf-auto`). It has no checkpoint, rewind, fork, sleep or snapshot: those raise
  `Kosa8UnsupportedError` (501 `not_available`) with the server's reason.

### Cloud errors

| Error | When | What to do |
|---|---|---|
| `Kosa8PaymentRequiredError` (402) | no payment method (`payment_required`), spending cap reached (`spending_cap_reached`), Store price not accepted | show `e.upgrade_url`; an agent should relay `e.agent_message` to its person and stop |
| `Kosa8OrgFrozenError` (423, `org_frozen`) | the org is frozen in the Security Center | an owner or admin unfreezes it; retrying does not help |
| `Kosa8RateLimitError` (429) | too many requests, or `quota_exceeded` (active sandbox limit) | wait `e.retry_after` seconds when set, or delete sandboxes |
| `Kosa8ExecTimeoutError` (504 `exec_timeout`, or today's 502 "timed out") | a buffered exec ran past its 90 s limit | run it in the background and poll |
| `Kosa8UnsupportedError` (501) | the host has no data plane yet (files, checkpoints, fork, ports) | use `exec`, or try later |

Every Cloud error carries the server's stable `e.code` and its message, and
`e.failure_class` / `e.retryable` (see RL training below).
`KOSA8_CLOUD_URL` overrides the address (https only, except localhost).

Since 0.5.0 the client retries what is safe to send again: reads, deletes,
heartbeats and drains on infra failures (no answer, 5xx, host or capacity
codes), and any request on a 429, waiting at least its `Retry-After`. Waits
are exponential with full jitter, 3 tries by default. Creates, batches, forks
and grades carry an `Idempotency-Key` that their retries reuse, so they are
retried on infra failures too and kosa8 Cloud answers a retry with the first
answer. Other POSTs (an exec, recording rollouts) that failed with a 5xx are
**not** resent, since they may have run. Since 0.6.0 every request, those
POSTs included, is also retried on refusals made before anything ran: 503
`database_busy`, 409 `template_building` (waiting its `retry_after_s`, the
build's ETA, up to 60 s) and 409 `idempotency_in_progress`; and any read on an
answer whose body says `retryable: true`. The wait honours the `Retry-After`
header and the body's `retry_after` / `retry_after_s`.
`Kosa8Cloud(retry=kosa8.RetryPolicy(attempts=5))` changes it;
`retry=kosa8.NO_RETRY` turns it off.


### Your own templates (Dockerfile builds, bulk image import)

Build once, then every sandbox restores in about a second instead of pulling the image:

```python
cloud.org_templates.build("swe-base", dockerfile=open("Dockerfile").read())
cloud.org_templates.import_images({"django-11099": "swebench/sweb.eval.x86_64.django_1776_django-11099@sha256:…"})
cloud.org_templates.wait("swe-base")          # raises TemplateBuildError with the log on failure
box = cloud.sandboxes.create(template="swe-base")
```

## RL training

`kosa8.rl` runs agent rollouts for RL and post-training: fan out from one
prepared sandbox, grade each rollout in a sealed fork with tests the agent
never sees, keep infra failures out of the gradient, and never leak a
sandbox. Each run shows up at app.kosa8.com under **RL runs**.

```python
import kosa8
from kosa8.rl import Run
cloud = kosa8.Cloud()
with Run(cloud, "my-grpo-run") as run:
    base = cloud.sandboxes.create(template="python")
    results = run.map(base, tasks, solve, grade_cmd=["python", "/grader/test.py"], concurrency=64)
```

Here `tasks` is a list (dicts with a hidden `files` map such as
`{"/grader/test.py": GRADER}`), and `solve(sandbox, task)` runs your agent in
its own fork of `base`. `run.map` does one step. For each rollout it forks
`base` (forks are made up to 100 per call), calls `solve`, grades the result
with `grade_cmd` in a fork that has no network and the task's hidden files,
records the rollout on the run, and deletes the sandbox. The results come
back in task order as `Rollouts`, with `.rewards`, `.mask`, `.groups()` (for
GRPO advantages), `.mean_reward` and `.by_class`. Each `RolloutResult` has
`reward`, `failure_class`, `masked`, `metrics`, `stdout`, `attempts` and
`sandbox_id`. See [examples/rl/quickstart.py](examples/rl/quickstart.py).

| Option | |
|---|---|
| `group_size=8` | samples per task (GRPO's G); results stay grouped by task |
| `base=` | a sandbox to fork, a `snap_` id, create options (`{"image": ...}`), or `make(task)` (one image per task, as in SWE-bench) |
| `grade_cmd=`, `files=` | the sealed grader. Its reward is the last stdout line that is JSON with a numeric `"reward"` (plus optional `"metrics"`), else exit 0 scores 1.0 and anything else 0.0 |
| `reward=fn(sandbox, task, output)` | your own reward, in place of a grader; `solve` may also return a number or `{"reward": x}` |
| `retries=2` | infra failures and 429s are retried on a fresh sandbox, with backoff and jitter. A failed grade is retried on its own first, so the agent does not run again |
| `mask=("infra", "quota", "error")` | these failure classes come back `masked=True, reward=None`, never 0. Add `"timeout"` for DeepSWE-style filtering |
| `timeout_s=` | caps `solve`; past it, the sandbox is deleted and the result is `timeout` |
| `keep=True` | keeps solved sandboxes until the run drains them (to inspect or fork them) |

**No leaks.** Inside `with Run(...)`, every sandbox the client makes is
labelled `kosa8.run=<id>` and leased (`lease_s`, default 600). A background
heartbeat keeps the lease alive. Leaving the block drains the run's sandboxes,
whether the exit is normal, an exception, Ctrl-C, SIGTERM or interpreter
exit. If the process is killed outright, kosa8 Cloud deletes the sandboxes
`lease_s` after the last heartbeat. Without a `Run`, use
`kosa8.rl.lease(cloud, labels={...})` as a context manager.

**Failure classes.** Every API error and exec answer carries a
`failure_class`, so a trainer can mask the rollout instead of scoring it 0:

| Class | Means | Retryable |
|---|---|---|
| `ok` / `exit_nonzero` | the command or grader ran (it passed or failed) | - |
| `timeout` | an exec or grader ran past its limit | no |
| `infra` | host failed, not ready, no capacity, 5xx, no answer | yes (not a 501: the host lacks the feature) |
| `quota` | running or plan limit, payment (402), frozen org; a 429 rate limit | only 429 |
| `client` | a bad request, 404, 409, 410, 413 or 422 | no (except `409 idempotency_in_progress`) |
| `error` | your own code raised (SDK-side class) | no |

`kosa8.classify_failure(exc)` gives `(failure_class, retryable)` for any
exception, and `kosa8.failure_of(status, code)` the server's rule for an HTTP
status and error code. A rollout whose own code raised (`error`) is recorded on
the run as `client`, with `sdk_failure_class: "error"` in its meta.

**Building blocks.** These work on both `kosa8.Cloud` and `kosa8.AsyncCloud`:

```python
batch = cloud.sandboxes.create_many(256, template="python", run_id=run.id, lease_s=600)  # .failed lists the rest
kids = base.fork(300, labels={"step": "4"}, lease_s=600)    # 3 calls of 100; all deleted if one fails
g = kids[0].grade(["pytest", "-q", "/grader"], files={"/grader/test_x.py": b"..."}, run_id=run.id,
                  rollout={"step": 4, "group": 0, "index": 0})   # g.reward, g.metrics, g.failure_class
cloud.sandboxes.heartbeat(run_id=run.id)            # what lease() sends for you
cloud.sandboxes.drain(labels={"job": "exp-7"})      # delete every match
results = cloud.fork_map(base, fn, items, concurrency=32, timeout_s=300)   # fn(child, item), children always deleted
run.record({"step": 4, "reward": 0.5, "failure_class": "ok"})   # buffered, sent 500 at a time
run.summary(); run.rollouts(step=4, failure_class="infra"); run.export("run.jsonl")
```

**Async.** `kosa8.AsyncCloud` is the asyncio twin. It uses `httpx` with a
pooled client when `pip install 'kosa8[async]'` is installed, and otherwise
the standard library in threads. `async with` on a sandbox deletes it.
`AsyncRun` and `kosa8.rl.map_rollouts(solve, tasks, base=..., run=...)` run
the same loop with `async def solve` ([examples/rl/async_rollouts.py](examples/rl/async_rollouts.py)).

**Frameworks.** Each adapter imports its framework only when it is used:

| Module | Plugs into |
|---|---|
| `kosa8.rl.trl` | TRL `GRPOTrainer(environment_factory=environment_factory(cloud, base, grade_cmd=...), reward_funcs=reward_func)`. Tools are `bash` and `write_file`; a masked episode's reward is `None`, which TRL leaves out of the loss ([grpo_trl.py](examples/rl/grpo_trl.py)) |
| `kosa8.rl.verifiers` | Prime Intellect verifiers v1, as a custom runtime: `import_path = "kosa8.rl.verifiers.Kosa8Backend"` ([verifiers_env.py](examples/rl/verifiers_env.py)) |
| `kosa8.rl.nemo_gym` | NeMo Gym, as the sandbox provider `kosa8` (registered by entry point) ([nemo_gym.yaml](examples/rl/nemo_gym.yaml)) |
| `kosa8.rl.openenv` | OpenEnv: `make_environment(...)` gives an `Environment`, and `create_app(...)` serves one sandbox per session |
| `kosa8.rl.gym` | a Gymnasium-style `SandboxEnv`: the actions are shell commands and `submit` grades |
| `kosa8.rl.swebench` | SWE-bench: `create` (the instance image), `apply_patch`, `grade` and `tasks` with the hidden tests ([swebench_grade.py](examples/rl/swebench_grade.py)) |

Limits to plan around: a buffered exec (the grader's command included) runs
at most 90 s, and an exec request (command and env) is at most 64 KiB. A
grade takes at most 64 hidden files, adding up to 8 MiB before base64 (the SDK
raises `ValueError` past that); a recorded rollout keeps 8 KiB of `meta` and
4 KiB of stdout. One fork call makes up to 1000 copies (100 at a time on the
parent's host) and one batch call makes 1000; a lease is 60 s to 24 hours.
Your plan's running limit also applies: past it, a batch or fork reports the
rest as `running_limit` in `.failed`, and one that made none raises the first
refusal (with `requested`, `created: 0` and `failed` in the error's `.body`).

The sealed grader's 90 s cap stays for now: split a long test suite into
several grades (`swebench.grade` raises `GradeTooLong` with that advice).
Longer grading comes with the upcoming kosa8 Jobs API.

**Safe retries.** Creates, batches, forks and grades send an
`Idempotency-Key` (a new UUID, or `idempotency_key=` yours) that every retry
reuses; kosa8 Cloud keeps the answer for 24 hours and replays it. So the SDK
retries those calls after a timeout or a 5xx too, and a retried grade records
its rollout once. The same key with another body is `422
idempotency_mismatch`; a duplicate that arrives while the first is still
running waits for it, or gets `409 idempotency_in_progress`, which the SDK
retries. A grade given a run (inside `Run`, or `run_id=`) is recorded by kosa8
Cloud itself (`GradeResult.recorded`), so `Run.map` does not record it again.
A grade whose command did not finish has `reward=None` and failure class
`timeout` or `infra`: masked, never scored 0.

### Hosted jobs: kosa8 runs the rollouts

When you only have tasks, a solver command and a grader, `cloud.jobs` hands
the whole loop to kosa8 Cloud: every task gets its own sandbox, the solver
runs there (as a background command, so it may run for hours) with the
task's input in `$KOSA8_TASK_INPUT` and a file, the sealed grader scores a
copy with no network and the hidden files, and each rollout lands in an RL
run (step 0, group = the task id). Infra failures are retried
`retries_on_infra` times, then recorded masked. The job keeps going if your
laptop closes.

```python
est = cloud.jobs.estimate(name="swe-eval", template="python", tasks="tasks.jsonl",
                          solver={"cmd": "python /workspace/solve.py", "timeout_s": 1800},
                          grader={"cmd": "pytest -q /hidden/test.py", "timeout_s": 60},
                          grader_files={"/hidden/test.py": pathlib.Path("tests/test.py")})
print(est["sandbox_hours"], est["usd_list"], est["included_hours_left"])   # {"low", "high"} ranges
job = cloud.jobs.submit(name="swe-eval", template="python", tasks="tasks.jsonl",
                        solver={"cmd": "python /workspace/solve.py", "timeout_s": 1800},
                        grader="pytest -q /hidden/test.py",
                        grader_files={"/hidden/test.py": pathlib.Path("tests/test.py")},
                        concurrency=64, budget_usd=20)
final = cloud.jobs.wait(job["id"], progress=lambda j: print(j["status"], j["progress"]))
```

`cloud.jobs.get`, `.list`, `.cancel` (stops new rollouts and deletes the
job's sandboxes) and the same calls on `AsyncCloud` are there too. A job
past `budget_usd` stops with status `budget_reached`.

Compare runs and load rollouts into a dataframe (pandas and pyarrow are
optional and imported only when asked for):

```python
import pandas as pd
pd.DataFrame(Run.compare([run_a, "run_…"], cloud=cloud))
df = Run.attach(cloud, final["run_id"]).rollouts(as_="pandas")   # or "arrow", "records"
```

## Quickstart: local daemon

```python
from kosa8 import Kosa8Client

k = Kosa8Client()
k.create_sandbox(name="demo", egress="deny")   # a microVM with its own kernel, no network
sb = k.sandbox("demo")

sb.pull("alpine:latest")
print(sb.run("alpine:latest", ["sh", "-c", "echo hello"]).stdout)

sb.checkpoint(label="before the risky bit")    # memory, processes and disk
sb.run("alpine:latest", ["sh", "-c", "rm -rf /data"])
sb.rewind()                                    # back, processes resumed

forks = sb.fork(4)                             # four live copies
sb.delete()
```

The local daemon needs macOS on Apple silicon or Linux with KVM (`kosa8` CLI installed).

## What's in it

| Area | Methods |
|---|---|
| Status | `health`, `version` |
| Sandboxes | `create_sandbox`, `list_sandboxes`, `get_sandbox`, `start_sandbox`, `stop_sandbox`, `delete_sandbox` |
| Images | `pull`, `pull_into_sandbox`, `sandbox_images` |
| Running code | `run`, `exec`, `exec_frames`, `start_container`, `start_interactive`, `write_stdin`, `attach` |
| Network | `sandbox_egress`; `egress="deny" \| "any" \| "fakenet"` or `egress_allow=[...]` on create |
| Snapshots | `snapshot`, `list_snapshots`, `restore_snapshot`, `delete_snapshot` |
| Fork | `fork(sandbox, n)`, `explore(sandbox, variants)` (best-of-N) |
| Checkpoints | `checkpoint`, `list_checkpoints`, `rewind` |

`k.sandbox(id)` returns a handle with the same methods bound to one sandbox.

Checkpoints share the CLI's store (`~/.kosa8/checkpoints.json`, under the same
`flock`), so `kosa8 rewind` sees checkpoints taken here and the other way round.

## Toolbox

```python
client = Kosa8Client()
sbx = client.sandbox("agent-1")
r = sbx.run_code("import math; print(math.pi)")          # CodeResult: stdout, exit_code, artifacts
sbx.shell("cd /workspace && git init demo")               # cd and export persist
sbx.write_file("demo/app.py", "print('hi')")
print(sbx.read_text("demo/app.py"))
sbx.shell("cd demo && python3 -m http.server 8000 >/dev/null 2>&1 &")
print(sbx.preview(8000)["url"])                           # signed, expiring link
client.create_sandbox(name="scratch", ephemeral=True, auto_stop_min=15)
```

## Switching from E2B, Daytona or Modal

Change one import; the rest of your code stays. Set `KOSA8_API_KEY` (a `k8c_live_` key):

```python
from kosa8.compat.e2b import Sandbox                    # was: from e2b import Sandbox
from kosa8.compat.e2b_code_interpreter import Sandbox   # was: from e2b_code_interpreter import Sandbox
from kosa8.compat.daytona import Daytona, CreateSandboxFromImageParams   # was: from daytona import ...
import kosa8.compat.modal as modal                      # was: import modal

sbx = Sandbox.create(timeout=600)                       # kosa8's python template, like E2B's default image
print(sbx.commands.run("echo hi").stdout)
sbx.files.write("app.py", "print(6 * 7)")               # relative paths go under /workspace
print(sbx.commands.run("python3 /workspace/app.py").stdout)   # 42
sbx.kill()

from kosa8.compat.e2b_code_interpreter import Sandbox as CodeSandbox
with CodeSandbox.create() as nb:                         # persistent: the interpreter keeps Python running
    nb.run_code("x = 6 * 7")
    print(nb.run_code("x").text)                         # 42
```

The E2B shims start kosa8's `python` template when you give no template (or E2B's `base`), since
E2B's default image has Python and kosa8's `base` is Alpine without it. The code-interpreter
`Sandbox` is created `persistent`, which `run_code` needs; it counts against your plan's
persistent-sandbox limit.

| Shim (mirrors) | What maps onto kosa8 Cloud |
|---|---|
| `kosa8.compat.e2b` (`e2b` 2.64.0, `e2b-code-interpreter` 2.11.10) | `Sandbox.create`, `connect`, `kill`, `set_timeout`, `get_info`, `is_running`, `pause`, `fork`, `create_snapshot` / `list_snapshots` / `delete_snapshot`, `Sandbox.list`, `download_url`; `commands.run` (foreground and `background=True`), `connect`, `kill`; `files.read` / `write` / `write_files` / `list` / `exists` / `get_info` / `remove` / `rename` / `make_dir`; `run_code` and code contexts (Python) |
| `kosa8.compat.daytona` (`daytona` 2.1.0) | `Daytona().create` (snapshot / image params, `Resources`), `get`, `list`, `delete`, `start`, `stop`; `sandbox.process.exec` / `code_run`; sessions (`create_session`, `execute_session_command` with `run_async`, `get_session_command`, `get_session_command_logs`, `get_session_command_logs_async`, `get_session`, `list_sessions`, `delete_session`); `sandbox.fs` upload / download / list / info / folders / permissions / move / delete; `sandbox.fork`, `create_snapshot`, `set_labels`, `set_autostop_interval`, `set_ttl`, `get_preview_link`; `daytona.snapshot.list` / `get` / `delete` |
| `kosa8.compat.modal` (`modal` 1.6.4) | `App.lookup`, `Image.from_registry` / `debian_slim` / `from_id`, `Secret.from_dict` / `from_name`; `Sandbox.create(*entrypoint, ...)`, `exec`, `wait`, `poll`, `returncode`, `terminate`, `stdout` / `stderr` reading, `snapshot_filesystem`, `from_id`, `from_name`, `list`, `set_tags` / `get_tags`; `sandbox.filesystem` read / write / list / stat / make_directory / remove / copy |

What kosa8 cannot do raises `kosa8.compat.Unsupported`, naming the method and the kosa8 way to do it
(for example Modal's `Image.pip_install`: kosa8 boots images, it does not build them). A shim never
pretends a call worked. Errors come back as the other SDK's exceptions (`e2b.TimeoutException`,
`DaytonaNotFoundError`, `modal.exception.NotFoundError`), with the kosa8 error as `__cause__`.
Every shim object has `.kosa8`, the `CloudSandbox` underneath, for anything else.

How kosa8 differs, whichever shim you use: only `/workspace` is sure to outlive a command (unless
the sandbox is persistent). Output streams where the other SDK streams it, as a streamed exec that
is not held to 90 s: E2B `commands.run(..., on_stdout=, on_stderr=)` or with a `timeout` over 90 s
(or 0), Modal `sandbox.exec(...)` (iterate `process.stdout` for lines as they print), and Daytona
session commands (`get_session_command_logs_async`) or `process.exec(timeout=)` over 90 s. A plain
foreground command (no callbacks, no long timeout) is buffered: it runs at most 90 s and its output
arrives when it ends. A background command (E2B `background=True`, a Modal entrypoint) is polled
about once a second and its new output passed on as it appears, stdout and stderr together (a
Modal entrypoint's stderr is kept apart in a file). Daytona session commands each run in a fresh
container, so `cd` and `export` do not carry between them. No stdin, PTY, GPU, volumes or image builds. Async variants (`AsyncSandbox`,
`AsyncDaytona`, `.aio`) are not mirrored. `kosa8.compat.set_default_cloud(Kosa8Cloud(...))` points
every shim at one client.

**Which path to use.** kosa8 Cloud also answers E2B's own HTTP API at `https://api.kosa8.com/e2b`
(the server side, `workers/cloud-api/src/e2b.js` in the kosa8 repo). Use that when you cannot change
code at all, or the code is not Python: keep the official `e2b` SDK (any language) and set
`E2B_API_KEY=k8c_live_...`, `E2B_API_URL=https://api.kosa8.com/e2b` and
`E2B_SANDBOX_URL=https://api.kosa8.com/e2b/envd`. It covers create, list, get, kill, set_timeout,
pause, connect, `files.read` / `write` and `commands.run`; fork, snapshots, `files.list`, metrics,
PTY and stdin answer 501, and env vars, metadata and `cpu_count` on create are refused. Use
`kosa8.compat.e2b` when you can change one import: it reaches more (env and metadata on create,
fork, snapshots, `files.list` / `exists` / `get_info` / `remove`, background commands, the code
interpreter) and names the kosa8 alternative when something is missing. Daytona and Modal have
only the Python shims. For new code, use `Kosa8Cloud` directly.

## Agent frameworks

These adapters drive the local daemon (`Kosa8Client`). For kosa8 Cloud with LangChain,
Inspect or Harbor, install the adapter from PyPI (source on GitHub under kosa8ai):

```sh
pip install langchain-kosa8   # deepagents / LangChain sandbox backend
pip install inspect-kosa8     # Inspect AI: sandbox="kosa8"
pip install harbor-kosa8      # Harbor: -e harbor_kosa8:Kosa8Environment
```

```python
from kosa8.integrations.openai_agents import sandbox_tools   # pip install 'kosa8[openai-agents]'
agent = Agent(name="coder", tools=sandbox_tools(client, "agent-1"))

from kosa8.integrations.langchain import sandbox_tools       # pip install 'kosa8[langchain]'
from langchain.agents import create_agent                    # LangChain 1.x
agent = create_agent(model, tools=sandbox_tools(client, "agent-1"))
```

Guides: https://docs.kosa8.com/guides/py-frameworks/ and https://docs.kosa8.com/guides/py-rl-eval/.

## Connecting

| Setting | Default |
|---|---|
| `socket_path=` | `~/.kosa8/run/kosa8.sock` |
| `host=` / `KOSA8_HOST` | a remote worker: `https://…`, `tcp://host:port`, `unix:///path` |
| `token=` / `KOSA8_TOKEN` | the worker's bearer token |
| `insecure=` / `KOSA8_INSECURE=1` | allow plain http to a non-loopback host (off) |
| `actor=` | `tool`; recorded in the audit log as `human`, `agent` or `tool` |
| `agent_token=` / `KOSA8_AGENT_TOKEN` | agent identity for the audit log |
| `timeout=` | seconds per connect and read; `None` waits |
| `worker_file=` | `~/.kosa8/worker.json`, the CLI's saved worker connection; `False` reads none |

Where requests go, in order: `host=` or `KOSA8_HOST`; else the connection the
CLI saved (`kosa8 wsl setup` on Windows, where the engine runs in WSL2, or
`kosa8 worker join` anywhere), with its token; else the local daemon's socket.
So on Windows, once `kosa8 wsl setup` has run, `Kosa8Client()` reaches the WSL2
engine with no configuration.

## The same thing in each language

The five kosa8 SDKs (Python `kosa8`, JS `@kosa8/sdk`, Go `kosa8-go`, Ruby `kosa8`, Java
`com.kosa8:kosa8`) call the same kosa8 Cloud API, but some names differ by language. The ones
that catch people switching:

| | Python | JS / TS | Go | Ruby | Java |
|---|---|---|---|---|---|
| client | `Kosa8Cloud()` | `new Kosa8Cloud()` | `kosa8.NewCloud(kosa8.CloudOptions{})`, returns `(cloud, err)` | `Kosa8::Cloud.new` | `Kosa8Cloud.builder().build()` |
| lifetime on create | `ttl_s=3600` | `{ ttlS: 3600 }` | `CloudCreateOptions{TTLSeconds: 3600}` | `ttl_s: 3600` | `new CloudOptions.Create().ttlSeconds(3600)` |
| persistent sandbox | `persistent=True` | `{ persistent: true }` | `CloudCreateOptions{Persistent: true}` | `persistent: true` | `.persistent(true)` |
| sandbox id | `box.id` | `box.id` | `box.ID()`, a method (`fmt.Println(box.ID)` prints a func value; `go vet` reports it) | `box.id` | `box.id()` |
| shell command | `box.exec("ls /")` | `box.exec('ls /')` | `box.ExecShell(ctx, "ls /", nil)` | `box.exec("ls /")` | `box.exec("ls /")` |
| argv command | `box.exec(["ls", "/"])` | `box.exec(['ls', '/'])` | `box.Exec(ctx, []string{"ls", "/"}, nil)` | `box.exec(["ls", "/"])` | `box.exec(List.of("ls", "/"))` |
| per-exec timeout | `exec(cmd, timeout_s=30)` | `exec(cmd, { timeoutS: 30 })` | `&kosa8.CloudExecOptions{TimeoutS: 30}` | `exec(cmd, timeout_s: 30)` | `exec(cmd, null, null, 30)` |
| exit code | `r.exit_code` | `r.exitCode` | `r.ExitCode`, an `*int` (nil when the cloud sent none; `r.OK()` checks for 0) | `r.exit_code` | `r.exitCode()`, an `Integer` (may be null) |
| files | `box.files.write` / `read_text` / `list` | `box.files.write` / `readText` / `list` | `box.Files().WriteString` / `ReadText` / `List(ctx, "")` | `box.files.write` / `read_text` / `list` | `box.files().write` / `readText` / `list()` |
| no such sandbox | `Kosa8NotFoundError` | `Kosa8NotFoundError` | `*kosa8.Error` with `Kind` `KindNotFound` (`errors.Is(err, kosa8.ErrNotFound)`) | `Kosa8::NotFoundError` | `Kosa8Exception.NotFoundException` |

In every SDK a non-zero exit is returned in the result, not raised. Only `/workspace` is sure to
persist between commands, unless the sandbox is persistent.

## Errors

Every daemon error is raised as a typed exception carrying the daemon's own
message and remediation: `Kosa8BadRequestError` (400), `Kosa8AuthError` (401),
`Kosa8PolicyError` (403), `Kosa8ApprovalRequiredError` (403 needing a signed
approval: repeat the call with `approval=<id>`), `Kosa8NotFoundError` (404),
`Kosa8ConflictError` (409), `Kosa8UnsupportedError` (501),
`Kosa8ConnectionError`, `Kosa8StreamError` (output kept), `Kosa8TimeoutError`.

## Tests

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

## License

MIT
