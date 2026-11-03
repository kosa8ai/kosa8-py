"""The sandbox toolbox: run code, files and git, shell sessions, terminals,
previews, lifecycle and templates. Mixed into :class:`kosa8.Kosa8Client`.

Every method maps to one daemon route (see https://docs.kosa8.com/guides/native-api/).
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from .errors import Kosa8StreamError
from .transport import enc


@dataclass
class Artifact:
    """A file a code run produced: today, a chart as a PNG."""

    type: str
    name: str
    data: bytes


@dataclass
class CodeResult:
    """The outcome of :meth:`Kosa8Client.run_code`. A non-zero ``exit_code`` is not an error."""

    exit_code: int
    stdout: str
    stderr: str
    language: str = ""
    image: str = ""
    artifacts: List[Artifact] = field(default_factory=list)
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class ShellResult:
    """The outcome of one command in a session. ``cwd`` is where the shell is now."""

    exit_code: int
    stdout: str
    stderr: str
    cwd: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class GitResult:
    """The outcome of a git operation; ``status``, ``log`` or ``branches`` are filled for those ops."""

    op: str
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    path: str = ""
    status: Optional[Dict[str, Any]] = None
    log: Optional[List[Dict[str, Any]]] = None
    branches: Optional[List[Dict[str, Any]]] = None


def _code_result(d: Mapping[str, Any]) -> CodeResult:
    return CodeResult(
        exit_code=int(d.get("exit") or 0),
        stdout=d.get("stdout") or "",
        stderr=d.get("stderr") or "",
        language=d.get("language") or "",
        image=d.get("image") or "",
        artifacts=[
            Artifact(a.get("type", ""), a.get("name", ""), base64.b64decode(a.get("data") or ""))
            for a in d.get("artifacts") or []
        ],
        duration_ms=int(d.get("duration_ms") or 0),
        timed_out=bool(d.get("timed_out")),
        truncated=bool(d.get("truncated")),
    )


def _tar_dir(path: str, dockerfile: str) -> bytes:
    """Pack a build context the way ``kosa8 build`` does, honouring ``.dockerignore`` prefixes."""
    ignore: List[str] = []
    di = os.path.join(path, ".dockerignore")
    if os.path.exists(di):
        with open(di) as f:
            ignore = [os.path.normpath(l.strip()) for l in f if l.strip() and not l.startswith("#")]
    if not os.path.exists(os.path.join(path, dockerfile)):
        raise FileNotFoundError(f"no {dockerfile} in {path}")

    def skip(rel: str) -> bool:
        return any(rel == p or rel.startswith(p + os.sep) for p in ignore)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for root, dirs, files in os.walk(path):
            rel_root = os.path.relpath(root, path)
            dirs[:] = [d for d in dirs if not skip(os.path.normpath(os.path.join(rel_root, d)))]
            for name in dirs + files:
                full = os.path.join(root, name)
                rel = os.path.normpath(os.path.join(rel_root, name))
                if skip(rel):
                    continue
                tf.add(full, arcname=rel.replace(os.sep, "/"), recursive=False)
    return buf.getvalue()


class ToolboxMixin:
    """Methods for the sandbox toolbox. ``self.transport`` is the client's transport."""

    transport: Any

    # --- code -------------------------------------------------------------------

    def run_code(
        self,
        sandbox: str,
        code: str,
        *,
        language: str = "python",
        image: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_s: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> CodeResult:
        """POST /sandboxes/{id}/code: run a Python, JavaScript or TypeScript snippet.

        It starts in ``/workspace``. Charts a Python snippet draws with matplotlib
        come back in ``artifacts`` as PNG bytes.
        """
        body: Dict[str, Any] = {"language": language, "code": code}
        if image:
            body["image"] = image
        if env:
            body["env"] = dict(env)
        if timeout_s:
            body["timeout_s"] = timeout_s
        d = self.transport.json(
            "POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/code", body=body, approval=approval,
            timeout=None if timeout_s is None else timeout_s + 120,
        )
        return _code_result(d)

    # --- files ------------------------------------------------------------------

    def list_files(self, sandbox: str, path: str = "/workspace") -> List[Dict[str, Any]]:
        """GET /sandboxes/{id}/files: a folder in the workspace (or one entry)."""
        d = self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files", query={"path": path})
        return (d or {}).get("files") or []

    def stat_file(self, sandbox: str, path: str) -> Dict[str, Any]:
        """GET /sandboxes/{id}/files/stat: one entry, not following a symlink."""
        return self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files/stat", query={"path": path})

    def read_file(self, sandbox: str, path: str) -> bytes:
        """GET /sandboxes/{id}/files/content: a file's bytes."""
        from .errors import error_from_response
        from .transport import _decode

        res = self.transport.raw("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files/content", query={"path": path})
        data = res.read()
        if res.status >= 400:
            raise error_from_response(res.status, _decode(data.decode("utf-8", "replace")),
                                      method="GET", path="files/content")
        want = res.headers.get("Content-Length")
        if want is not None and int(want) != len(data):
            raise Kosa8StreamError(f"{path}: got {len(data)} of {want} bytes", "", "")
        return data

    def read_text(self, sandbox: str, path: str, encoding: str = "utf-8") -> str:
        return self.read_file(sandbox, path).decode(encoding)

    def write_file(
        self, sandbox: str, path: str, data: Any, *, mode: Optional[str] = None, approval: Optional[str] = None
    ) -> Dict[str, Any]:
        """PUT /sandboxes/{id}/files/content: replace a file (bytes or str), making its folders."""
        from .errors import error_from_response
        from .transport import _decode

        q: Dict[str, Any] = {"path": path}
        if mode:
            q["mode"] = mode
        payload = data.encode() if isinstance(data, str) else bytes(data)
        res = self.transport.raw("PUT", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files/content", query=q,
                                 raw_body=payload, approval=approval)
        body = _decode(res.read().decode("utf-8", "replace"))
        if res.status >= 400:
            raise error_from_response(res.status, body, method="PUT", path="files/content")
        return body

    def delete_file(
        self, sandbox: str, path: str, *, recursive: bool = False, approval: Optional[str] = None
    ) -> Dict[str, Any]:
        """DELETE /sandboxes/{id}/files: a file, or with ``recursive`` a folder."""
        return self.transport.json("DELETE", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files",
                                   query={"path": path, "recursive": recursive}, approval=approval)

    def make_dir(self, sandbox: str, path: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files/mkdir",
                                   body={"path": path}, approval=approval)

    def move_file(self, sandbox: str, src: str, dst: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/files/move",
                                   body={"from": src, "to": dst}, approval=approval)

    # --- git --------------------------------------------------------------------

    def git(self, sandbox: str, op: str, *, approval: Optional[str] = None, **fields: Any) -> GitResult:
        """POST /sandboxes/{id}/git: ``op`` is clone, status, add, commit, push, pull,
        checkout, branches, log or diff; ``fields`` are that op's (``url``, ``path``,
        ``branch``, ``message``, ``token``…). A git failure returns ``ok=False``.
        """
        body = {"op": op, **{k: v for k, v in fields.items() if v is not None}}
        d = self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/git", body=body, approval=approval)
        return GitResult(
            op=d.get("op", op), ok=bool(d.get("ok")), exit_code=int(d.get("exit") or 0),
            stdout=d.get("stdout") or "", stderr=d.get("stderr") or "", path=d.get("path") or "",
            status=d.get("status"), log=d.get("log"), branches=d.get("branches"),
        )

    def git_clone(self, sandbox: str, url: str, path: Optional[str] = None, **kw: Any) -> GitResult:
        return self.git(sandbox, "clone", url=url, path=path, **kw)

    def git_status(self, sandbox: str, path: str, **kw: Any) -> GitResult:
        return self.git(sandbox, "status", path=path, **kw)

    def git_commit(self, sandbox: str, path: str, message: str, *, add_all: bool = True, **kw: Any) -> GitResult:
        """Stage everything (unless ``add_all=False``) and commit."""
        if add_all:
            r = self.git(sandbox, "add", path=path, approval=kw.get("approval"))
            if not r.ok:
                return r
        return self.git(sandbox, "commit", path=path, message=message, **kw)

    # --- sessions ---------------------------------------------------------------

    def create_session(
        self, sandbox: str, name: Optional[str] = None, *, image: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None, approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sandboxes/{id}/sessions: a shell that keeps ``cd`` and ``export`` between commands."""
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if image:
            body["image"] = image
        if env:
            body["env"] = dict(env)
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/sessions", body=body, approval=approval)

    def list_sessions(self, sandbox: str) -> List[Dict[str, Any]]:
        return self.transport.json("GET", f"/kosa8/v1/sandboxes/{enc(sandbox)}/sessions") or []

    def session_exec(
        self, sandbox: str, session: str, command: str, *, timeout_s: Optional[float] = None,
        approval: Optional[str] = None,
    ) -> ShellResult:
        """POST /sandboxes/{id}/sessions/{name}/exec: run one command line in the session."""
        body: Dict[str, Any] = {"command": command}
        if timeout_s:
            body["timeout_s"] = timeout_s
        d = self.transport.json(
            "POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/sessions/{enc(session)}/exec", body=body,
            approval=approval, timeout=None if timeout_s is None else timeout_s + 120,
        )
        return ShellResult(
            exit_code=int(d.get("exit") or 0), stdout=d.get("stdout") or "", stderr=d.get("stderr") or "",
            cwd=d.get("cwd") or "", duration_ms=int(d.get("duration_ms") or 0),
            timed_out=bool(d.get("timed_out")), truncated=bool(d.get("truncated")),
        )

    def shell(self, sandbox: str, command: str, *, session: str = "main", **kw: Any) -> ShellResult:
        """Run a command in a session, making the session on first use."""
        from .errors import Kosa8NotFoundError

        try:
            return self.session_exec(sandbox, session, command, **kw)
        except Kosa8NotFoundError as err:
            if "no such session" not in str(err):
                raise
        self.create_session(sandbox, session, approval=kw.get("approval"))
        return self.session_exec(sandbox, session, command, **kw)

    def delete_session(self, sandbox: str, session: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        return self.transport.json("DELETE", f"/kosa8/v1/sandboxes/{enc(sandbox)}/sessions/{enc(session)}",
                                   approval=approval)

    # --- terminals --------------------------------------------------------------

    def start_pty(
        self, sandbox: str, *, image: Optional[str] = None, command: Optional[Sequence[str]] = None,
        cols: int = 0, rows: int = 0, env: Optional[Mapping[str, str]] = None, approval: Optional[str] = None,
    ) -> str:
        """POST /sandboxes/{id}/pty: a container with a terminal; returns its id.

        Connect to ``/kosa8/v1/sandboxes/{id}/containers/{cid}/tty`` with any WebSocket
        client: binary messages are the terminal, text messages JSON control.
        """
        body: Dict[str, Any] = {}
        if image:
            body["image"] = image
        if command:
            body["command"] = list(command)
        if cols and rows:
            body["cols"], body["rows"] = cols, rows
        if env:
            body["env"] = dict(env)
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/pty", body=body, approval=approval)["id"]

    def resize_pty(self, sandbox: str, container: str, cols: int, rows: int) -> Dict[str, Any]:
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/containers/{enc(container)}/resize",
                                   body={"cols": cols, "rows": rows})

    # --- previews ---------------------------------------------------------------

    def create_preview(
        self, sandbox: str, port: int, *, session: Optional[str] = None, container: Optional[str] = None,
        expires_in_s: Optional[float] = None, approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sandboxes/{id}/previews: a signed link to a web server in the sandbox that expires."""
        body: Dict[str, Any] = {"port": port}
        if session:
            body["session"] = session
        if container:
            body["container"] = container
        if expires_in_s:
            body["expires_in_s"] = expires_in_s
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/previews", body=body, approval=approval)

    def revoke_previews(self, *, approval: Optional[str] = None) -> Dict[str, Any]:
        """POST /kosa8/v1/previews/rotate: end every preview link at once."""
        return self.transport.json("POST", "/kosa8/v1/previews/rotate", approval=approval)

    # --- lifecycle --------------------------------------------------------------

    def set_lifecycle(
        self, sandbox: str, *, auto_stop_min: Optional[int] = None, auto_delete_min: Optional[int] = None,
        ttl_min: Optional[int] = None, ephemeral: Optional[bool] = None, approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sandboxes/{id}/lifecycle: change auto-stop, auto-delete, TTL or ephemeral.
        Arguments left as ``None`` keep their value; 0 turns one off."""
        body = {k: v for k, v in {"auto_stop_min": auto_stop_min, "auto_delete_min": auto_delete_min,
                                  "ttl_min": ttl_min, "ephemeral": ephemeral}.items() if v is not None}
        return self.transport.json("POST", f"/kosa8/v1/sandboxes/{enc(sandbox)}/lifecycle", body=body, approval=approval)

    # --- templates --------------------------------------------------------------

    def list_templates(self) -> List[Dict[str, Any]]:
        return self.transport.json("GET", "/kosa8/v1/templates") or []

    def delete_template(self, name: str, *, approval: Optional[str] = None) -> Dict[str, Any]:
        return self.transport.json("DELETE", f"/kosa8/v1/templates/{enc(name)}", approval=approval)

    def build_template(
        self, name: str, context_dir: str = ".", *, dockerfile: str = "Dockerfile",
        build_args: Optional[Mapping[str, str]] = None, egress: Optional[str] = None, replace: bool = False,
        on_output: Optional[Callable[[bytes], None]] = None, approval: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /kosa8/v1/templates: build ``context_dir``'s Dockerfile in a throwaway
        sandbox and keep the image as a template. Returns the final frame."""
        q: List[tuple] = [("name", name), ("dockerfile", dockerfile)]
        for k, v in (build_args or {}).items():
            q.append(("buildarg", f"{k}={v}"))
        if egress:
            q.append(("egress", egress))
        if replace:
            q.append(("replace", "1"))
        ctx = _tar_dir(context_dir, dockerfile)
        last: Dict[str, Any] = {}
        for f in self.transport.frames("POST", "/kosa8/v1/templates?" + urlencode(q), raw_body=ctx,
                                       approval=approval, timeout=None):
            if f.get("done"):
                if f.get("error"):
                    raise Kosa8StreamError(f["error"], "", "", daemon_message=f["error"])
                last = f
                break
            if on_output and f.get("data"):
                on_output(f["data"])
        return last
