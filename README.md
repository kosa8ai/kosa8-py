# kosa8 for Python

Python client for [kosa8](https://kosa8.com): microVM sandboxes for AI agents, with
checkpoint, rewind, fork and explore. The counterpart of
[`@kosa8/sdk`](https://www.npmjs.com/package/@kosa8/sdk), method for method.

No dependencies. It talks to **kosa8 Cloud** (`https://api.kosa8.com`) with an API
key, or to the local kosa8 daemon over its unix socket, or to a remote kosa8 worker.

```sh
pip install "kosa8>=0.4"
```

## Quickstart: kosa8 Cloud

Make an API key in the dashboard (https://app.kosa8.com) and set `KOSA8_API_KEY`.

```python
from kosa8 import Kosa8Cloud

cloud = Kosa8Cloud()  # KOSA8_API_KEY from the environment; or Kosa8Cloud("k8c_live_...")

box = cloud.sandboxes.create(template="python", ttl_s=3600)   # deleted automatically after an hour
r = box.exec(["python3", "-c", "print(6 * 7)"])
print(r.exit_code, r.stdout)          # 0 42

box.exec("echo hello > /workspace/hello.txt")   # only /workspace persists between commands
print(box.exec("cat /workspace/hello.txt").stdout)

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

### How Cloud sandboxes run commands

- The `base` template is **Alpine Linux**: `sh` is busybox, and there is no `bash`,
  `python` or `git`. Use `template="python"` (or another key from `cloud.templates()`)
  for Python, or pass `image=` to `exec`.
- **Each `exec` runs in a fresh container, and only `/workspace` persists.** `cd`,
  `export` and anything installed outside `/workspace` (`apk add`, `pip install`
  into the system) are gone by the next command. Install into `/workspace`, for
  example `pip install --target /workspace/lib ...` with `PYTHONPATH=/workspace/lib`,
  or chain the steps in one `sh -c`.
- **A buffered `exec` is cut off after about 30 seconds** and raises
  `Kosa8ExecTimeoutError`. Run long work in the background and poll it:
  `box.exec("nohup sh -c 'make test > /workspace/test.log 2>&1; echo $? > /workspace/test.exit' &")`.
- A request body (command and env) is at most 64 KiB; a file read or write is at
  most 8 MiB.
- Set `ttl_s` (60 s to 90 days) or `delete_after` on create so a crashed script
  cannot leave a sandbox running.
- The Cloud API has no sandbox labels or metadata.

### Cloud errors

| Error | When | What to do |
|---|---|---|
| `Kosa8PaymentRequiredError` (402) | no payment method (`payment_required`), spending cap reached (`spending_cap_reached`), Store price not accepted | show `e.upgrade_url`; an agent should relay `e.agent_message` to its person and stop |
| `Kosa8OrgFrozenError` (423, `org_frozen`) | the org is frozen in the Security Center | an owner or admin unfreezes it; retrying does not help |
| `Kosa8RateLimitError` (429) | too many requests, or `quota_exceeded` (active sandbox limit) | wait `e.retry_after` seconds when set, or delete sandboxes |
| `Kosa8ExecTimeoutError` (504 `exec_timeout`, or today's 502 "timed out") | a buffered exec ran past about 30 s | run it in the background and poll |
| `Kosa8UnsupportedError` (501) | the host has no data plane yet (files, checkpoints, fork, ports) | use `exec`, or try later |

Every Cloud error carries the server's stable `e.code` and its message.
`KOSA8_CLOUD_URL` overrides the address (https only, except localhost).

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

## Agent frameworks

These adapters drive the local daemon (`Kosa8Client`). For kosa8 Cloud with LangChain,
Inspect or Harbor, see `langchain-kosa8`, `inspect-kosa8` and `harbor-kosa8` on GitHub (kosa8ai).

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
