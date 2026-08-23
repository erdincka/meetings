"""Entry point for the code-execution sandbox.

Reads a job from /work/in/job.json, runs the model-authored snippet, and writes
whatever it produced to /work/out. It never returns Python objects to the
caller -- only files and captured stdout -- so nothing the snippet builds can
influence the calling process directly.

This tier has no network at all. Its NetworkPolicy denies egress outright, so
the code cannot reach the database, the backend, the inference endpoint or the
internet, whatever it decides to try.
"""

from __future__ import annotations

import io
import json
import resource
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

WORK = Path("/work")
JOB = WORK / "in" / "job.json"
OUT = WORK / "out"
RESULT = OUT / "result.json"

# Belt and braces alongside the container's cgroup limits: a runaway allocation
# should fail inside the interpreter rather than getting the pod OOM-killed,
# because a clean Python traceback is far more useful to an agent than a
# vanished container.
ADDRESS_SPACE_LIMIT = 768 * 1024 * 1024
CPU_SECONDS_LIMIT = 45
MAX_STDOUT_CHARS = 20_000


def _apply_limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_LIMIT, ADDRESS_SPACE_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS_LIMIT, CPU_SECONDS_LIMIT))
    # No new files beyond what the workspace needs.
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    try:
        job = json.loads(JOB.read_text())
    except Exception as exc:
        RESULT.write_text(json.dumps({"ok": False, "error": f"unreadable job: {exc}"}))
        return 1

    code = job.get("code", "")
    inputs = job.get("inputs", {})

    # Inputs are materialised as files rather than injected as globals: the
    # snippet reads them the same way it would read anything else, and there is
    # no ambiguity about what the caller placed in scope.
    for name, content in inputs.items():
        (WORK / "in" / name).write_text(content)

    _apply_limits()

    # matplotlib must not look for a display, and must not try to write a font
    # cache into a read-only home.
    import os

    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", str(WORK / "mpl"))
    (WORK / "mpl").mkdir(exist_ok=True)

    stdout, stderr = io.StringIO(), io.StringIO()
    ok, error = True, None

    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            # __name__ set so `if __name__ == "__main__"` blocks behave.
            exec(compile(code, "<analysis>", "exec"), {"__name__": "__main__"})
    except BaseException as exc:  # noqa: BLE001 - report everything, including SystemExit
        ok = False
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        traceback.print_exc(file=stderr)

    produced = sorted(
        p.name for p in OUT.iterdir() if p.is_file() and p.name != RESULT.name
    )

    RESULT.write_text(
        json.dumps(
            {
                "ok": ok,
                "error": error,
                "stdout": stdout.getvalue()[:MAX_STDOUT_CHARS],
                "stderr": stderr.getvalue()[:MAX_STDOUT_CHARS],
                "files": produced,
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
