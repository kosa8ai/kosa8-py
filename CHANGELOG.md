# Changelog

## 0.3.0 — 2026-11-03

kosa8 Cloud's API (api.kosa8.cloud) is not open yet; the `Kosa8Cloud` client is ready for when it is.

Windows, and paired workers.

- With no `host=`, `KOSA8_HOST` or `socket_path=`, the client follows the CLI's saved worker connection (`~/.kosa8/worker.json`, written by `kosa8 wsl setup` and `kosa8 worker join`), token included, as the CLI does. On Windows that is the WSL2 engine; before, the SDK tried a unix socket, which Python on Windows does not have. `worker_file=False` turns it off.
- A connection failure on Windows says to run `kosa8 wsl setup`.
- CI runs the tests on Linux, macOS and Windows, Python 3.9 and 3.13.

kosa8 Cloud, the same as `@kosa8/sdk`'s `Kosa8Cloud`.

- `Kosa8Cloud`: a client for the kosa8 Cloud control plane, authenticated with a `k8c_live_` API key (`KOSA8_API_KEY`; address `KOSA8_CLOUD_URL`, default `https://api.kosa8.cloud`, https only except localhost). Standard library only.
- `cloud.sandboxes.create`, `list` and `get` return a `CloudSandbox` with `exec`, `sleep`, `wake`, `refresh` and `delete`.
- `cloud.functions`: `deploy`, `list`, `get`, `delete`, `invoke` and `map`.
- `cloud.me()` and `cloud.usage()`.
- Errors: every error has `code`, the cloud's stable error code. New `Kosa8PaymentRequiredError` (402) and `Kosa8RateLimitError` (429).

## 0.2.0 — 2026-11-03

The sandbox toolbox, and adapters for agent frameworks. Needs a kosa8 daemon with the
toolbox routes (the release after v0.12.1).

- **Code run:** `run_code(sandbox, code, language="python")` returns a `CodeResult` with
  stdout, stderr, the exit code and any matplotlib charts as PNG `artifacts`.
- **Files:** `list_files`, `stat_file`, `read_file`/`read_text`, `write_file`, `delete_file`,
  `make_dir`, `move_file`, all in the sandbox's `/workspace`.
- **Git:** `git(sandbox, op, ...)`, `git_clone`, `git_status`, `git_commit` (stages first).
- **Sessions:** `shell(sandbox, command)` keeps `cd` and `export` between calls and makes the
  session on first use; also `create_session`, `session_exec`, `list_sessions`, `delete_session`.
- **Terminals:** `start_pty` and `resize_pty`.
- **Previews:** `create_preview(sandbox, port)` returns a signed link that expires;
  `revoke_previews()` ends them all.
- **Lifecycle:** `create_sandbox` takes `auto_stop_min`, `ttl_min`, `auto_delete_min`,
  `ephemeral` and `template`; `set_lifecycle` changes them later.
- **Templates:** `build_template(name, context_dir)` builds a Dockerfile into a template
  (honouring `.dockerignore`), plus `list_templates` and `delete_template`.
- **`Sandbox`** gains `run_code`, `shell`, `list_files`, `read_file`, `read_text`,
  `write_file`, `delete_file`, `git`, `preview` and `set_lifecycle`.
- **Framework adapters:** `kosa8.integrations.openai_agents.sandbox_tools` (OpenAI Agents
  SDK) and `kosa8.integrations.langchain.sandbox_tools` (LangChain, LangGraph), on
  `kosa8.integrations.sandbox_functions`. Install with `kosa8[openai-agents]` or
  `kosa8[langchain]`. Tool errors come back to the model as text, with the daemon's remedy.

## 0.1.1 — 2026-11-03

Fixes found while writing and testing the docs at https://docs.kosa8.com/guides/py-guide/.

- **Concurrent checkpoints of one sandbox no longer collide.** Two `checkpoint()` calls at
  once used to pick the same `<sandbox>-ckpt-<n>` and one failed with
  `Kosa8ConflictError: snapshot already exists`. The loser now steps to the next id; a 409
  for any other reason is raised. Verified live with two threads.
- **A second `write_stdin` to the same container now raises `Kosa8Error`** instead of
  answering `{'ok': True}` for input the process never sees. The daemon treats one request
  body as the whole of stdin and closes it at the end; stream with an iterable.
- **A failed `rewind` raises `Kosa8CheckpointError` and says what happened**: the sandbox
  was deleted (not stopped) and the checkpoint is intact, with the `restore_snapshot` call
  that recovers it. (`Kosa8CheckpointError` is a `Kosa8Error`, so existing handlers still catch it.)
- **The checkpoint file lock waits at most 10 s** (`FileCheckpointStore(lock_timeout=...)`),
  as `@kosa8/sdk` does, then raises `Kosa8CheckpointError` instead of hanging.
- **A frame that is not valid NDJSON raises the new `Kosa8ProtocolError`** (a `Kosa8Error`),
  not `json.JSONDecodeError`.
- **`explore` no longer sleeps after its last retry.**
- **`Sandbox` gains `exec_frames`, `start_interactive`, `write_stdin` and `attach`.**

Not changed (daemon behaviour): a client-side `timeout` stops waiting but does not stop the
command; a missing snapshot on restore is a 500, not a 404; `exec_frames`' final frame
reports `ok` for the stream, with the command's status in `exit`.

## 0.1.0 — 2026-11-03

First release.
