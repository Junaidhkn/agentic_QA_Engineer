"""Stub server for external HTTP dependencies (ADR-0011). Requests to ``/<stub>/...`` get the
configured response for (method, path) or ``200 {}``; every call is recorded for the report so
users can see which integrations the app tried to reach."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_RECORDED = 500


class StubServer:
    def __init__(self, stubs: list[dict[str, Any]], host: str = "127.0.0.1", port: int = 0) -> None:
        self.routes: dict[tuple[str, str, str], dict[str, Any]] = {}
        for stub in stubs:
            for r in stub.get("routes", []):
                self.routes[(stub["name"], r.get("method", "GET").upper(), r["path"])] = r
        self.calls: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return None

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(min(length, 1024 * 1024))
                path = self.path.split("?", 1)[0]
                parts = path.lstrip("/").split("/", 1)
                name, rest = parts[0], "/" + (parts[1] if len(parts) > 1 else "")
                route = server.routes.get((name, self.command, rest))
                status = int(route.get("status", 200)) if route else 200
                body = json.dumps(route.get("body", {}) if route else {}).encode()
                key = f"{name} {self.command} {rest}"
                server.counts[key] = server.counts.get(key, 0) + 1
                if len(server.calls) < MAX_RECORDED:
                    server.calls.append({"stub": name, "method": self.command, "path": rest, "status": status})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle  # noqa: N815

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> StubServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def summary(self) -> list[dict[str, Any]]:
        return [{"call": k, "count": v} for k, v in sorted(self.counts.items(), key=lambda kv: -kv[1])][:50]


def main() -> None:  # container entry point: python -m runner_agent.sandbox_env.stub --config stubs.json
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args()
    with open(args.config) as fh:
        stubs = json.load(fh)
    server = StubServer(stubs, host="0.0.0.0", port=args.port)  # noqa: S104 - inside the sandbox network
    server.httpd.serve_forever()


if __name__ == "__main__":
    main()
