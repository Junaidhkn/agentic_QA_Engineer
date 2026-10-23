"""A tiny HTTP app used as a load-test target in end-to-end tests (deliberately racy /api/orders)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TargetApp:
    def __init__(self, port: int = 0, delay_s: float = 0.0) -> None:
        self.stock = 3
        self.delay_s = delay_s
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, obj: object) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if app.delay_s:
                    time.sleep(app.delay_s)
                if self.path.startswith("/.well-known/reliability-engineer.txt"):
                    return self._send(200, {"token": getattr(app, "token", "")})
                if self.path.startswith("/api/products/1"):
                    return self._send(200, {"id": 1, "stock": app.stock})
                return self._send(200, {"ok": True})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/login":
                    return self._send(200, {"token": "t0k"})
                if self.path == "/api/cart":
                    return self._send(201, {"id": "c1", "qty": body.get("qty")})
                if self.path == "/api/orders":
                    current = app.stock
                    time.sleep(0.01)  # read-modify-write race
                    app.stock = current - 1
                    return self._send(201, {"id": "o1"})
                return self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> TargetApp:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()

    async def __aenter__(self) -> TargetApp:
        return self.__enter__()

    async def __aexit__(self, *exc: object) -> None:
        self.__exit__(*exc)
