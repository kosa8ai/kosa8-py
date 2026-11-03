# Changelog

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
