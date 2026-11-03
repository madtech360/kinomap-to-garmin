#!/usr/bin/env python3
"""
Tiny HTTP trigger for kinomap_to_garmin.py (standard library only).

POST /run  with header "Authorization: Bearer <TRIGGER_TOKEN>"
  -> runs kinomap_to_garmin.py once and returns a short JSON result:
     {"ok": true,  "message": "Uploaded Elliptical", "trace_id": "3fa91c2e"}
     {"ok": false, "message": "1 upload(s) failed (trace 3fa91c2e)", "trace_id": "3fa91c2e"}

The full detail goes to the container log and to the rotating log file; every line of a
run carries the trace id:
     docker compose logs k2g | grep 3fa91c2e
     docker compose exec k2g grep 3fa91c2e /data/logs/k2g.log

GET /health -> {"status": "ok"}  (no auth, no side effects)

Environment:
    TRIGGER_TOKEN   required; shared secret that Home Assistant sends
    TRIGGER_PORT    default 8765
    SCRIPT_PATH     default: kinomap_to_garmin.py next to this file
    RUN_TIMEOUT     seconds before a run is killed, default 300
"""

import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = os.environ.get("TRIGGER_TOKEN", "")
PORT = int(os.environ.get("TRIGGER_PORT", "8765"))
SCRIPT = os.environ.get("SCRIPT_PATH", str(Path(__file__).with_name("kinomap_to_garmin.py")))
TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "300"))

run_lock = threading.Lock()  # only one run at a time


def say(text: str) -> None:
    """Server-side line in the container log (the uploader itself logs to the same stream)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S,%f}"[:-3] + f" INFO [server] {text}", file=sys.stderr, flush=True)


def run_once() -> dict:
    if not run_lock.acquire(blocking=False):
        return {"ok": False, "message": "A run is already in progress, try again shortly.", "trace_id": None}
    run_id = uuid.uuid4().hex[:8]
    fd, summary_path = tempfile.mkstemp(prefix="k2g-", suffix=".json")
    os.close(fd)
    say(f"[{run_id}] run started")
    try:
        # stdout/stderr are inherited, so the uploader's log lines land in the container log
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--summary-file", summary_path],
            env=dict(os.environ, RUN_ID=run_id), timeout=TIMEOUT,
        )
        try:
            data = json.loads(Path(summary_path).read_text())
            result = {"ok": bool(data["ok"]), "message": str(data["message"]), "trace_id": run_id}
        except (OSError, ValueError, KeyError):
            result = {"ok": False, "trace_id": run_id,
                      "message": f"Failed: the run ended unexpectedly, exit code {proc.returncode} "
                                 f"(trace {run_id})"}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "trace_id": run_id,
                  "message": f"Failed: timed out after {TIMEOUT}s (trace {run_id})"}
    finally:
        Path(summary_path).unlink(missing_ok=True)
        run_lock.release()
    say(f"[{run_id}] run finished: ok={result['ok']} - {result['message']}")
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        return bool(TOKEN) and hmac.compare_digest(header.encode(), expected.encode())

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        self._send(200, run_once())

    def log_message(self, fmt, *args):  # one tidy line per request, never headers
        say(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    if not TOKEN or len(TOKEN) < 16:
        sys.exit("Set TRIGGER_TOKEN to a random string of at least 16 characters")
    if TOKEN.lower().startswith("change-me"):
        sys.exit("TRIGGER_TOKEN is still the example placeholder; generate your own: openssl rand -hex 24")
    say(f"listening on 0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
