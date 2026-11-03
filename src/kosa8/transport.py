"""HTTP over the daemon's unix socket (or a remote worker). Standard library only.

Follows the kosa8 CLI's rules exactly, as ``@kosa8/sdk``'s transport.ts does:
the default endpoint is ``$HOME/.kosa8/run/kosa8.sock``; ``KOSA8_HOST`` (or
``host=``) selects a remote worker, with ``KOSA8_TOKEN`` as its bearer token;
plain http to a non-loopback host is refused unless ``insecure`` (or
``KOSA8_INSECURE=1``).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Union
from urllib.parse import quote, urlencode, urlsplit

from .errors import Kosa8ConnectionError, Kosa8Error, Kosa8ProtocolError, Kosa8TimeoutError, error_from_response

APPROVAL_HEADER = "X-Kosa8-Approval"
ACTOR_HEADER = "X-Kosa8-Actor"
AGENT_TOKEN_HEADER = "X-Kosa8-Agent-Token"

Body = Union[bytes, str, Iterable[Union[bytes, str]]]
Query = Mapping[str, Union[str, int, bool, None]]


def default_socket_path() -> str:
    """The daemon's default socket: ``$HOME/.kosa8/run/kosa8.sock`` (pkg/paths)."""
    return os.path.join(os.path.expanduser("~"), ".kosa8", "run", "kosa8.sock")


def enc(segment: str) -> str:
    """Percent-encode one path segment (encodeURIComponent)."""
    return quote(segment, safe="")


def _is_loopback(hostname: str) -> bool:
    h = hostname.strip("[]")
    return h in ("localhost", "::1") or h.startswith("127.")


@dataclass(frozen=True)
class Endpoint:
    kind: str  # "unix" | "tcp"
    socket_path: str = ""
    scheme: str = ""
    host: str = ""
    port: Optional[int] = None
    base_path: str = ""
    token: Optional[str] = None

    @property
    def target(self) -> str:
        if self.kind == "unix":
            return f"unix://{self.socket_path}"
        port = f":{self.port}" if self.port else ""
        return f"{self.scheme}://{self.host}{port}"


def default_worker_file() -> str:
    """The CLI's saved worker connection: ``$HOME/.kosa8/worker.json`` (internal/cli/worker.go)."""
    return os.path.join(os.path.expanduser("~"), ".kosa8", "worker.json")


def _load_saved_worker(path: str) -> Optional[Dict[str, Any]]:
    """The saved connection, or None when there is none or it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            w = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(w, dict):
        return None
    host = w.get("host")
    if not isinstance(host, str) or not host.strip():
        return None
    token = w.get("token")
    return {
        "host": host.strip(),
        "token": token if isinstance(token, str) and token else None,
        "insecure": w.get("insecure") is True,
    }


def resolve_endpoint(
    host: Optional[str] = None,
    socket_path: Optional[str] = None,
    token: Optional[str] = None,
    insecure: bool = False,
    env: Optional[Mapping[str, str]] = None,
    worker_file: Union[str, bool, None] = None,
) -> Endpoint:
    """Resolve where to connect, as the CLI does: ``host`` or ``KOSA8_HOST``;
    else the CLI's saved worker connection (``worker_file``, default
    ``~/.kosa8/worker.json``, written by ``kosa8 wsl setup`` and ``kosa8 worker
    join``; ``False`` reads none); else the daemon's unix socket."""
    env = os.environ if env is None else env
    from_env = (env.get("KOSA8_HOST") or "").strip()
    raw = (host if host is not None else from_env).strip()
    allow_plain = insecure or env.get("KOSA8_INSECURE") == "1"
    if raw:
        # A saved token is never sent to a host named here.
        if token is None and host is None and from_env:
            token = env.get("KOSA8_TOKEN") or None
        return _parse_host(raw, allow_plain, token)
    if socket_path is None and worker_file is not False and worker_file != "":
        saved = _load_saved_worker(worker_file if isinstance(worker_file, str) else default_worker_file())
        if saved:
            return _parse_host(saved["host"], allow_plain or saved["insecure"], token if token is not None else saved["token"])
    return Endpoint(kind="unix", socket_path=socket_path or default_socket_path())


def _parse_host(raw: str, allow_plain: bool, token: Optional[str]) -> Endpoint:
    h = raw = raw.rstrip("/")
    i = h.find("://")
    if i > 0:
        h = h[:i].lower() + h[i:]
    if h.startswith("unix://"):
        return Endpoint(kind="unix", socket_path=h[len("unix://"):])
    if h.startswith("tcp://"):
        h = "http://" + h[len("tcp://"):]
    elif not (h.startswith("http://") or h.startswith("https://")):
        if "://" in h:
            raise Kosa8Error(
                f'KOSA8_HOST scheme "{h[:h.index("://")]}" is not supported; '
                "use http://, https://, tcp:// or unix://"
            )
        h = "http://" + h
    u = urlsplit(h)
    if not u.hostname:
        raise Kosa8Error(f"KOSA8_HOST {raw!r} names no host")
    if u.scheme == "http" and not allow_plain and not _is_loopback(u.hostname):
        hp = u.netloc
        raise Kosa8Error(
            f"refusing to talk to {hp} over plain http: the token and every request would cross "
            f"the network unencrypted. Use https://{hp} (a worker behind a TLS-terminating tunnel "
            "or proxy), or set insecure=True / KOSA8_INSECURE=1 if this link is already private and trusted"
        )
    return Endpoint(
        kind="tcp",
        scheme=u.scheme,
        host=u.hostname,
        port=u.port,
        base_path=u.path.rstrip("/"),
        token=token or None,
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: Optional[float]) -> None:
        super().__init__("kosa8", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        if not hasattr(socket, "AF_UNIX"):  # Python on Windows
            raise FileNotFoundError(f"this Python has no unix sockets to reach {self._socket_path}")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            s.settimeout(self.timeout)
        s.connect(self._socket_path)
        self.sock = s


class Response:
    """A response whose body has not been read yet. Close it, or use ``with``."""

    def __init__(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse) -> None:
        self._conn = conn
        self._resp = resp
        self.status: int = resp.status
        self.headers = resp.headers

    def read(self) -> bytes:
        try:
            return self._resp.read()
        finally:
            self.close()

    def lines(self) -> Iterator[bytes]:
        try:
            while True:
                line = self._resp.readline()
                if not line:
                    return
                yield line
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _decode(text: str) -> Any:
    if text.strip() == "":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def decode_frame(line: Union[bytes, str]) -> Dict[str, Any]:
    """One NDJSON frame, with its base64 ``data`` decoded to bytes."""
    f = json.loads(line)
    if isinstance(f.get("data"), str):
        f["data"] = base64.b64decode(f["data"])
    return f


class Transport:
    """Speaks HTTP to kosa8d. A new connection per request: the daemon is local and cheap to reach."""

    def __init__(
        self,
        *,
        socket_path: Optional[str] = None,
        host: Optional[str] = None,
        token: Optional[str] = None,
        insecure: bool = False,
        actor: str = "tool",
        agent_token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        worker_file: Union[str, bool, None] = None,
    ) -> None:
        env = os.environ if env is None else env
        self.endpoint = resolve_endpoint(host, socket_path, token, insecure, env, worker_file)
        if actor not in ("human", "agent", "tool"):
            raise Kosa8Error(f"actor must be human, agent or tool, not {actor!r}")
        self.actor = actor
        self.agent_token = agent_token if agent_token is not None else (env.get("KOSA8_AGENT_TOKEN") or None)
        self.extra_headers = dict(headers or {})
        #: Default socket timeout in seconds for connect and each read; None waits forever.
        self.timeout = timeout

    @property
    def target(self) -> str:
        """Human-readable description of where requests go."""
        return self.endpoint.target

    def _path(self, p: str, query: Optional[Query]) -> str:
        out = self.endpoint.base_path + p
        if query:
            q = {}
            for k, v in query.items():
                if v is None or v is False:
                    continue
                q[k] = "1" if v is True else str(v)
            if q:
                out += "?" + urlencode(q)
        return out

    def _connection(self, timeout: Optional[float]) -> http.client.HTTPConnection:
        e = self.endpoint
        if e.kind == "unix":
            return _UnixHTTPConnection(e.socket_path, timeout)
        if e.scheme == "https":
            return http.client.HTTPSConnection(e.host, e.port, timeout=timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(e.host, e.port, timeout=timeout)

    def raw(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Query] = None,
        body: Any = None,
        raw_body: Optional[Body] = None,
        approval: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        """Send a request and return the response once its headers arrive."""
        h: Dict[str, str] = {"Accept": "application/json", ACTOR_HEADER: self.actor}
        h.update(self.extra_headers)
        h.update(headers or {})
        if approval and approval.strip():
            h[APPROVAL_HEADER] = approval.strip()
        if self.agent_token:
            h[AGENT_TOKEN_HEADER] = self.agent_token
        if self.endpoint.kind == "tcp" and self.endpoint.token:
            h["Authorization"] = f"Bearer {self.endpoint.token}"
        payload: Any = None
        if raw_body is not None:
            h["Content-Type"] = "application/octet-stream"
            if isinstance(raw_body, (bytes, bytearray, str)):
                payload = raw_body.encode() if isinstance(raw_body, str) else bytes(raw_body)
                h["Content-Length"] = str(len(payload))
            else:
                h["Transfer-Encoding"] = "chunked"
                payload = (c.encode() if isinstance(c, str) else c for c in raw_body)
        elif body is not None:
            payload = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            h["Content-Length"] = str(len(payload))
        elif method not in ("GET", "HEAD"):
            h["Content-Length"] = "0"
        full = self._path(path, query)
        conn = self._connection(self.timeout if timeout is None else timeout)
        try:
            chunked = h.get("Transfer-Encoding") == "chunked"
            if chunked:
                conn.putrequest(method, full, skip_accept_encoding=True)
                for k, v in h.items():
                    conn.putheader(k, v)
                conn.endheaders()
                for chunk in payload:
                    if chunk:
                        conn.send(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                conn.send(b"0\r\n\r\n")
            else:
                conn.request(method, full, body=payload, headers=h)
            resp = conn.getresponse()
        except socket.timeout as err:
            conn.close()
            raise Kosa8TimeoutError(f"{method} {path} timed out talking to {self.target}", method=method, path=path) from err
        except OSError as err:
            conn.close()
            hint = ""
            if isinstance(err, (FileNotFoundError, ConnectionRefusedError)):
                if self.endpoint.kind == "unix" and os.name == "nt":
                    hint = " (on Windows the engine runs in WSL2: run `kosa8 wsl setup`, or set KOSA8_HOST)"
                else:
                    hint = " (is kosa8 installed and its daemon running? start it with `kosa8 daemon start`)"
            raise Kosa8ConnectionError(
                f"kosa8 daemon unreachable at {self.target}: {err}{hint}", method=method, path=path
            ) from err
        return Response(conn, resp)

    def json(self, method: str, path: str, **kw: Any) -> Any:
        """Send a request and decode a JSON answer; HTTP errors become typed errors."""
        res = self.raw(method, path, **kw)
        try:
            text = res.read().decode("utf-8", "replace")
        except socket.timeout as err:
            raise Kosa8TimeoutError(f"{method} {path} timed out", method=method, path=path) from err
        body = _decode(text)
        if res.status >= 400:
            raise error_from_response(res.status, body, method=method, path=path)
        return body

    def frames(self, method: str, path: str, **kw: Any) -> Iterator[Dict[str, Any]]:
        """Send a request whose answer is NDJSON frames (run, exec, attach).

        A refusal before streaming began becomes a typed error; each frame's
        base64 ``data`` is decoded to bytes.
        """
        res = self.raw(method, path, **kw)
        if res.status >= 400:
            body = _decode(res.read().decode("utf-8", "replace"))
            raise error_from_response(res.status, body, method=method, path=path)
        try:
            for line in res.lines():
                s = line.strip()
                if s:
                    try:
                        frame = decode_frame(s)
                    except (ValueError, TypeError) as err:  # JSON or base64
                        raise Kosa8ProtocolError(
                            f"{method} {path}: the daemon sent a frame that is not valid NDJSON ({err}): {s[:200]!r}",
                            method=method,
                            path=path,
                        ) from err
                    yield frame
        except socket.timeout as err:
            raise Kosa8TimeoutError(f"{method} {path} timed out mid-stream", method=method, path=path) from err
        finally:
            res.close()
