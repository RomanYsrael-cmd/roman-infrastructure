#!/usr/bin/env python3
"""Tiny stateless HTTP service used only to validate the deployment path."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        healthy = os.environ.get("SMOKE_HEALTH_MODE", "healthy") == "healthy"
        body = json.dumps(
            {
                "status": "ok" if healthy else "unhealthy",
                "service": "roman-infrastructure-smoke",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
