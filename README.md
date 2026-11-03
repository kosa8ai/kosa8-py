# kosa8 for Python

Python client for [kosa8](https://kosa8.com): microVM sandboxes for AI agents, with
checkpoint, rewind, fork and explore. The counterpart of
[`@kosa8/sdk`](https://www.npmjs.com/package/@kosa8/sdk), method for method.

No dependencies. It talks to the local kosa8 daemon over its unix socket, or to a
remote kosa8 worker.

```sh
pip install kosa8
```

## Quickstart

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

```python
from kosa8.integrations.openai_agents import sandbox_tools   # pip install 'kosa8[openai-agents]'
agent = Agent(name="coder", tools=sandbox_tools(client, "agent-1"))

from kosa8.integrations.langchain import sandbox_tools       # pip install 'kosa8[langchain]'
agent = create_react_agent(model, sandbox_tools(client, "agent-1"))
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
