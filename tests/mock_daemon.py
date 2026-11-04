"""A minimal stand-in for kosa8d on a unix socket, for unit tests (as kosa8-js test/mock-daemon.ts)."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional


class Recorded:
    def __init__(self, method: str, url: str, headers: Dict[str, str], body: bytes) -> None:
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body or b"null")


Handler = Callable[[Recorded, BaseHTTPRequestHandler], None]


#: Python on Windows has no unix sockets; there the mock listens on loopback
#: TCP, which is also how the SDK reaches the WSL2 engine on Windows.
HAVE_UNIX = hasattr(socket, "AF_UNIX")

if HAVE_UNIX:

    class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

else:

    class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):  # type: ignore[no-redef]
        daemon_threads = True


class MockDaemon:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="k8m-")
        self.socket_path = os.path.join(self.dir, "d.sock")
        self.requests: List[Recorded] = []
        self.routes: List[tuple] = []
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def address_string(self) -> str:  # unix sockets have no client address
                return "unix"

            def _read_body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    out = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            return out
                        out += self.rfile.read(size)
                        self.rfile.readline()
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _handle(self) -> None:
                rec = Recorded(self.command, self.path, dict(self.headers), self._read_body())
                mock.requests.append(rec)
                p = self.path.split("?")[0]
                for method, pattern, handler in mock.routes:
                    if method == self.command and pattern.match(p):
                        handler(rec, self)
                        return
                send_json(self, 404, {"error": f"no route {self.command} {p}", "remediation": ""})

            do_GET = do_POST = do_DELETE = do_PUT = _handle

        if HAVE_UNIX:
            self.server = _Server(self.socket_path, H)
            #: Keyword arguments that point a Kosa8Client at this mock.
            self.client_args: Dict[str, Any] = {"socket_path": self.socket_path}
        else:
            self.server = _Server(("127.0.0.1", 0), H)
            self.client_args = {"host": f"http://127.0.0.1:{self.server.server_address[1]}"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def on(self, method: str, pattern: str, handler: Handler) -> "MockDaemon":
        rx = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", pattern) + "$")
        self.routes.insert(0, (method, rx, handler))
        return self

    def start(self) -> "MockDaemon":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        try:
            os.unlink(self.socket_path)
            os.rmdir(self.dir)
        except OSError:
            pass

    def find(self, method: str, prefix: str) -> List[Recorded]:
        return [r for r in self.requests if r.method == method and r.url.startswith(prefix)]


def send_json(h: BaseHTTPRequestHandler, code: int, body: Any) -> None:
    data = (json.dumps(body) + "\n").encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def send_frames(h: BaseHTTPRequestHandler, frames: List[Dict[str, Any]], chunked: bool = True) -> None:
    """Write NDJSON frames as the daemon does (data base64-encoded), chunked like a stream."""
    h.send_response(200)
    h.send_header("Content-Type", "application/x-ndjson")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()
    for f in frames:
        f = dict(f)
        if isinstance(f.get("data"), (bytes, str)):
            d = f["data"].encode() if isinstance(f["data"], str) else f["data"]
            f["data"] = base64.b64encode(d).decode()
        line = (json.dumps(f) + "\n").encode()
        h.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
        h.wfile.flush()
    h.wfile.write(b"0\r\n\r\n")
