# Changelog

## 0.5.0: RL training (unreleased)

`kosa8.rl` adds RL rollouts at scale. It needs the kosa8 Cloud API routes for
batch create, leases, drain, grade and RL runs.

- **`kosa8.rl.Run`**: `with Run(cloud, "name") as run:` creates the run (or attaches to `run_id`), and `run.map(base, tasks, solve, grade_cmd=...)` runs one step. Each rollout forks `base`, runs `solve`, grades in a sealed fork with the task's hidden files, is recorded and is deleted. Infra failures are retried, and anything still failing comes back `masked=True` with `reward=None`, never 0. `record` (batched, 500 a call), `flush`, `finish`, `summary`, `rollouts`, `export`, `drain`. There is also `AsyncRun`.
- **Leases**: `kosa8.rl.lease(...)` / `Lease` send heartbeats on a thread and drain on exit, on an exception, on Ctrl-C, on SIGTERM and at interpreter exit. Inside a `Run`, every sandbox the client makes is labelled `kosa8.run=<id>` and leased.
- **`kosa8.rl.map_rollouts`**: the asyncio rollout engine (`group_size`, `retries`, `timeout_s`, `mask`, `keep`).
- **`kosa8.AsyncCloud`**: the async client. It uses `httpx` (`pip install 'kosa8[async]'`) or the standard library in threads, with sandboxes, exec, files, fork, grade, batch, heartbeat, drain and `async with` cleanup.
- **On both clients**: `sandboxes.create_many(n, ...)` (`POST /v1/sandboxes/batch`, returns a `SandboxBatch` with `.failed`), `create(lease_s=, run_id=)`, `box.fork(n, labels=, run_id=, lease_s=, partial_ok=)` (more than 1000 is split into several calls, and the copies are deleted if one call fails), `sandboxes.heartbeat(...)`, `sandboxes.drain(...)`, `box.grade(cmd, files=, run_id=, rollout=)` returning a `GradeResult`, `cloud.fork_map(base, fn, items, concurrency=)` returning a `ForkResult` per item, and `with box:` deletes the sandbox.
- **Failure classes**: every error has `.failure_class` and `.retryable` (the server's own, else worked out from the status and code), and `CloudExecResult.failure_class` is set. Also `kosa8.classify_failure`.
- **Retries**: `RetryPolicy` (exponential backoff with full jitter that honours `Retry-After`) applies to safe requests by default: reads, deletes, heartbeats and drains on infra failures, creates, forks and grades too (they carry an Idempotency-Key), and any request on a 429. `retry=kosa8.NO_RETRY` turns it off. A create whose wait or labels fail now deletes the sandbox before raising.
- **Adapters** (lazy imports): `kosa8.rl.trl` (TRL `environment_factory`), `kosa8.rl.verifiers` (verifiers v1 custom runtime), `kosa8.rl.nemo_gym` (NeMo Gym sandbox provider, with an entry point), `kosa8.rl.openenv` (OpenEnv `Environment` and `create_app`), `kosa8.rl.gym` (Gymnasium-style env) and `kosa8.rl.swebench`.
- **Idempotency-Key**: create, create_many, fork and grade send one (a new UUID, or `idempotency_key=`) and reuse it on every retry, so they are retried after timeouts and 5xx too, and a retried grade records its rollout once. `409 idempotency_in_progress` is retried after its `retry_after_s`; `422 idempotency_mismatch` is raised.
- **Matches the kosa8 Cloud RL API**: `GradeResult` has `reward=None` when the grader's command did not finish (and `reward_from`, `retryable`, `kept`, `sandbox_id`, `run_id`, `recorded`); a graded rollout kosa8 Cloud recorded is not recorded again by `Run`. Grade files are checked here (at most 64, 8 MiB in all before base64) and `timeout_s` is 1 to 90 whole seconds. Rollout rows keep 8 KiB of meta and 4 KiB of stdout. A fork call makes up to 1000; a lease is 60 s to 24 hours; `drain` follows `more` and also returns `ids`. A batch or fork that made none raises the first refusal. Failure classes without a server `failure_class` follow the server's rules (`kosa8.failure_of`).
- `kosa8.Cloud` is an alias of `Kosa8Cloud`. New examples are in `examples/rl/`.
- **Changed**: `box.fork(n)` now accepts more than 100 and returns a `SandboxBatch`, which is a `list`.

The rest of the kosa8 Cloud API, and a fix that makes the Cloud client work at all.

- **Fix:** every Cloud request now sends `User-Agent: kosa8-python/<version>`. The API's edge refuses Python-urllib's default User-Agent with a bare 403 (Cloudflare error 1010), which 0.4.0 raised as `Kosa8PolicyError` on every call.

- **Sized and Docker creates:** `cloud.sandboxes.create` takes `size` (`CLOUD_SIZES`), `cpus` + `mem_mb`, `image`, `disk_gb`, `persistent`, `docker`, `cpu="dedicated"`, `name`, `env`, `egress`, `secrets`, `labels` and `snapshot`, and waits for a cold boot (`wait=False` to skip; `box.wait_until_running()`). Conflicting options raise `ValueError` before any request. `list()` takes `name` and `labels`.
- **Background commands:** `box.exec_background(cmd)` → `CloudBackgroundExec` with `status()` and `wait()`; `box.get_exec(id)`.
- **Snapshots:** `box.snapshot(name)`, `cloud.snapshots.create|list|get|delete`.
- **More of a sandbox:** `box.previews` (create, list, link, rotate, delete), `box.terminal()`, `box.run_code()` and `box.code_contexts`, `box.files.read_raw|write_raw|link`, `box.egress|set_egress|reset_egress`, `box.schedules`, `box.set_labels|metrics|logs|secrets`; `cloud.egress_presets|org_egress|set_org_egress|capabilities`.
- **Secrets:** `cloud.secrets.set|list|delete` (a value goes in a body only, never comes back).
- **Pipelines:** `cloud.pipelines.run|list|get|logs|cancel|artifacts|download_artifact|wait`, `cloud.pipelines.hooks.create|list|delete`.

## 0.4.0 — 2026-11-15

kosa8 Cloud parity: the Cloud client now reaches the whole sandbox API.

- **Address:** the default is `https://api.kosa8.com`, now pinned by a test. Do not use 0.3.0, which defaulted to `api.kosa8.cloud`, a domain kosa8 does not use.
- **Lifetime:** `cloud.sandboxes.create` takes `ttl_s` (60 s to 90 days, checked before sending), `delete_after` (Unix ms) and `accept_price` (paid Store items). New `CloudSandbox.update(ttl_s=, delete_after=, sleep_after_s=, clear_lifetime=)` (PATCH).
- **Paging:** `cloud.sandboxes.list` takes `limit` and `cursor`; new `list_page()` returns a `CloudSandboxPage` with `next_cursor`, and `iter()` walks every page. A server that does not page yet answers everything in one page, which works the same.
- **Files:** `box.files.read`, `read_text`, `write`, `list`, `mkdir`, `delete(recursive=)` (at most 8 MiB a file).
- **Checkpoints and fork:** `box.checkpoint(label)`, `box.checkpoints()`, `box.rewind(checkpoint_id)` (returns a `CloudRewindResult` with the automatic `backup`), `box.fork(n)` and `box.ports()`. Files, checkpoints, fork and ports need the host's data plane: on a host without it they raise `Kosa8UnsupportedError` (501).
- `cloud.templates()`.
- **Errors:** `Kosa8PaymentRequiredError` exposes `upgrade_url` and `agent_message`. New `Kosa8OrgFrozenError` (423 `org_frozen`) and `Kosa8ExecTimeoutError` (504 `exec_timeout`, or today's 502 "timed out" after about 30 s; also a `Kosa8TimeoutError`). `Kosa8RateLimitError.retry_after` when the server sends Retry-After. An opener may now return `(status, body, headers)`.
- README: Cloud quickstart first; how Cloud sandboxes run commands (Alpine `base`, fresh container per exec, only `/workspace` persists, the 30 s exec limit, 64 KiB bodies); the LangChain example uses `langchain.agents.create_agent`.

## 0.3.1 — 2026-11-03

- The default kosa8 Cloud address is now `https://api.kosa8.com` (0.3.0 defaulted to `api.kosa8.cloud`, which kosa8 Cloud does not use). `KOSA8_CLOUD_URL` still overrides it.

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
