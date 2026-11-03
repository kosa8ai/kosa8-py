"""Plain tool functions bound to one sandbox; the framework adapters wrap these."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence

from ..errors import Kosa8Error

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Kosa8Client

#: Every tool name, in the order adapters list them.
DEFAULT_TOOLS = ("run_code", "shell", "read_file", "write_file", "list_files", "git", "preview")

# Output an agent sees is capped so one noisy command cannot fill its context.
_MAX_OUT = 20_000


def _clip(s: str) -> str:
    if len(s) <= _MAX_OUT:
        return s
    half = _MAX_OUT // 2
    return s[:half] + f"\n… [{len(s) - _MAX_OUT} characters cut] …\n" + s[-half:]


def _report(exit_code: int, stdout: str, stderr: str, extra: str = "") -> str:
    parts = [f"exit code: {exit_code}{extra}"]
    if stdout:
        parts.append("stdout:\n" + _clip(stdout))
    if stderr:
        parts.append("stderr:\n" + _clip(stderr))
    return "\n".join(parts)


def _safe(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn a kosa8 error into text the model can read and act on, not an exception
    that ends the run. Policy refusals read as such, with the daemon's remedy."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):  # type: ignore[no-untyped-def]
        try:
            return fn(*a, **kw)
        except Kosa8Error as err:
            msg = f"error: {err.daemon_message or err.message}"
            if err.remediation:
                msg += f"\nwhat to do: {err.remediation}"
            return msg

    return wrapper


def sandbox_functions(
    client: "Kosa8Client",
    sandbox: str,
    *,
    tools: Optional[Sequence[str]] = None,
    session: str = "agent",
    approval: Optional[str] = None,
) -> List[Callable[..., str]]:
    """Return one function per tool, bound to ``sandbox``.

    Each has type hints and a docstring the frameworks turn into a tool schema,
    and returns text. A kosa8 error comes back as text starting ``error:`` so the
    model can recover. ``shell`` commands share one session (``session``), so
    ``cd`` and ``export`` carry from one call to the next. ``tools`` picks a
    subset of :data:`DEFAULT_TOOLS`.
    """
    want = list(tools or DEFAULT_TOOLS)
    unknown = [t for t in want if t not in DEFAULT_TOOLS]
    if unknown:
        raise ValueError(f"unknown tools {unknown}; choose from {list(DEFAULT_TOOLS)}")

    def run_code(code: str, language: str = "python") -> str:
        """Run a snippet of code in the sandbox and return its output.

        Args:
            code: The source to run. It starts in /workspace, where files persist.
            language: python, javascript or typescript.
        """
        r = client.run_code(sandbox, code, language=language, approval=approval)
        extra = " (timed out)" if r.timed_out else ""
        if r.artifacts:
            extra += f"; {len(r.artifacts)} chart(s): " + ", ".join(a.name for a in r.artifacts)
        return _report(r.exit_code, r.stdout, r.stderr, extra)

    def shell(command: str) -> str:
        """Run a shell command line in the sandbox. cd and export persist between calls.

        Args:
            command: The command line, run by sh. The shell starts in /workspace.
        """
        r = client.shell(sandbox, command, session=session, approval=approval)
        extra = " (timed out)" if r.timed_out else ""
        if r.cwd:
            extra += f"; cwd {r.cwd}"
        return _report(r.exit_code, r.stdout, r.stderr, extra)

    def read_file(path: str) -> str:
        """Read a text file from the sandbox workspace.

        Args:
            path: A path under /workspace, or relative to it.
        """
        data = client.read_file(sandbox, path)
        try:
            return _clip(data.decode("utf-8"))
        except UnicodeDecodeError:
            return f"{path} is binary ({len(data)} bytes); not shown"

    def write_file(path: str, content: str) -> str:
        """Create or replace a text file in the sandbox workspace, making its folders.

        Args:
            path: A path under /workspace, or relative to it.
            content: The whole new content of the file.
        """
        fi = client.write_file(sandbox, path, content, approval=approval)
        return f"wrote {fi.get('size', len(content.encode()))} bytes to {fi.get('path', path)}"

    def list_files(path: str = "/workspace") -> str:
        """List a folder in the sandbox workspace: one entry per line, folders end in /.

        Args:
            path: A folder under /workspace, or relative to it.
        """
        files = client.list_files(sandbox, path)
        if not files:
            return "(empty)"
        lines = []
        for f in files:
            name = f.get("name", "")
            if f.get("type") == "dir":
                lines.append(name + "/")
            else:
                lines.append(f"{name}  {f.get('size', 0)} bytes")
        return "\n".join(lines)

    def git(op: str, path: str = "", url: str = "", message: str = "", branch: str = "") -> str:
        """Run a git operation on a repository in the sandbox workspace.

        Args:
            op: clone, status, add, commit, push, pull, checkout, branches, log or diff.
            path: The repository's folder in the workspace (for clone, where to put it).
            url: The repository URL, for clone.
            message: The commit message, for commit.
            branch: The branch, for clone, checkout, push or pull.
        """
        fields = {k: v for k, v in {"path": path, "url": url, "message": message, "branch": branch}.items() if v}
        r = client.git(sandbox, op, approval=approval, **fields)
        out = _report(r.exit_code, r.stdout, r.stderr, "" if r.ok else " (failed)")
        for k in ("status", "log", "branches"):
            v = getattr(r, k)
            if v:
                out += f"\n{k}: " + _clip(json.dumps(v, indent=1))
        return out

    def preview(port: int) -> str:
        """Get a signed, expiring link to a web server listening on a port in the sandbox.

        Args:
            port: The TCP port the server listens on inside the sandbox.
        """
        p = client.create_preview(sandbox, port, session=session, approval=approval)
        return f"{p.get('url', '')} (expires {p.get('expires_at', p.get('expires', 'soon'))})"

    made: Dict[str, Callable[..., str]] = {
        "run_code": run_code, "shell": shell, "read_file": read_file, "write_file": write_file,
        "list_files": list_files, "git": git, "preview": preview,
    }
    return [_safe(made[t]) for t in want]
