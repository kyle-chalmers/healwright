"""Minimal webhook receiver sketch. Not shipped as part of v0.1; see README.md in this folder."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.environ.get("HEALWRIGHT_HEALER_DIR", "../../templates/healer"))
import healwright_core as hw  # noqa: E402

USERNAME = os.environ.get("RELAY_USERNAME", "")
PASSWORD = os.environ.get("RELAY_PASSWORD", "")
CONFIG = os.environ.get("HEALWRIGHT_CONFIG", "config.yaml")


def _authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic ") or not USERNAME or not PASSWORD:
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, USERNAME) and hmac.compare_digest(pw, PASSWORD)


def _handle(payload: dict) -> None:
    run = payload.get("run") or {}
    run_id = run.get("parent_run_id") or run.get("run_id")
    if not run_id:
        return
    cfg = hw.load_config(CONFIG)
    store = hw.make_store(cfg)
    platform = hw.DatabricksPlatform(tenant=cfg.get("tenant", ""))
    item = platform.fetch_run(int(run_id), source="webhook")
    if item is not None:
        hw.Healer(cfg, store, platform=platform).handle(item)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path != "/databricks" or not _authorized(self.headers.get("Authorization")):
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(202)
        self.end_headers()
        threading.Thread(target=_handle, args=(payload,), daemon=True).start()

    def log_message(self, fmt, *args):  # quiet: payloads never go to stdout
        return


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
