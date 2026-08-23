"""Job server for the code-execution sandbox.

A deliberately tiny HTTP surface -- POST a job, get the result -- rather than
using the Agent Sandbox SDK's file and command transport. The SDK's helper is
not part of this image, and depending on its internals to move a job around
would couple the security boundary to an implementation detail.

The exec tier has no egress at all. This server is the only way in, it accepts
exactly one shape of request, and everything it runs is confined to /work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WORK = Path("/work")
IN = WORK / "in"
OUT = WORK / "out"
RUNNER = "/opt/runner.py"

# Hard ceiling regardless of what the caller asks for. The persona sandbox also
# applies a timeout, but a sandbox that trusts its caller's timeout has no
# timeout at all.
MAX_WALL_SECONDS = 90
MAX_BODY_BYTES = 512 * 1024


class JobHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/run":
            self._reply(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            self._reply(413, {"error": "job too large"})
            return

        try:
            job = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            self._reply(400, {"error": f"malformed job: {exc}"})
            return

        IN.mkdir(parents=True, exist_ok=True)
        OUT.mkdir(parents=True, exist_ok=True)
        # Each job starts from a clean output directory, so one analysis cannot
        # collect a previous one's files and claim them as its own.
        for stale in OUT.iterdir():
            if stale.is_file():
                stale.unlink()

        (IN / "job.json").write_text(json.dumps(job))

        try:
            subprocess.run(
                [sys.executable, RUNNER],
                timeout=MAX_WALL_SECONDS,
                capture_output=True,
                cwd=str(WORK),
            )
        except subprocess.TimeoutExpired:
            self._reply(200, {"ok": False, "error": f"timed out after {MAX_WALL_SECONDS}s"})
            return

        try:
            result = json.loads((OUT / "result.json").read_text())
        except Exception as exc:
            self._reply(200, {"ok": False, "error": f"no result produced: {exc}"})
            return

        # Files come back inline; the tier has no shared storage by design.
        import base64

        artifacts: dict[str, str] = {}
        for name in result.get("files", []):
            raw = (OUT / name).read_bytes()
            artifacts[name] = base64.b64encode(raw).decode()
        result["artifacts"] = artifacts

        self._reply(200, result)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("exec-server: " + fmt % args + "\n")


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    HTTPServer(("0.0.0.0", 8080), JobHandler).serve_forever()
