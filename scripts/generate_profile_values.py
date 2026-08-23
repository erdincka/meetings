"""Generate the chart's profile values from the Python profile definitions.

The profiles decide two things that must never disagree: which tools the runtime
will register, and which Kubernetes objects get created (ServiceAccount,
SandboxTemplate, NetworkPolicy, Secret mounts, RBAC bindings). Maintaining that
in two places invites exactly the drift this project is meant to make
impossible -- a persona whose UI says it can run code, and a cluster that
silently disagrees.

So app/orchestration/profiles.py is the single source of truth, and this emits
the values fragment the chart consumes. CI regenerates and diffs, so a change to
one without the other fails the build.

    scripts/generate-profile-values.sh
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.orchestration.profiles import PROFILES  # noqa: E402

VALUES = REPO / "deploy" / "charts" / "meetings" / "values.yaml"
MARKER = "# GENERATED FILE -- do not edit."

HEADER = """# GENERATED FILE -- do not edit.
#
# Source: backend/app/orchestration/profiles.py
# Regenerate: scripts/generate-profile-values.sh
#
# Each entry becomes a ServiceAccount, a capability ConfigMap, a SandboxTemplate
# with its own NetworkPolicy, and (where warmReplicas > 0) a SandboxWarmPool.
# `canExecuteCode` is the one that matters most: it decides whether the profile's
# ServiceAccount is bound to the exec-sandbox-claimer Role, and therefore whether
# the apiserver will let its sandboxes claim a code-execution sandbox at all.
profiles:
"""


def main() -> int:
    lines = [HEADER]
    for profile in PROFILES:
        lines.append(f"  - name: {profile.name}")
        lines.append(f"    description: {profile.description!r}")
        lines.append(f"    canExecuteCode: {str(profile.can_execute_code).lower()}")
        lines.append(f"    needsMetricsDsn: {str(profile.needs_metrics_dsn).lower()}")
        lines.append(f"    needsCorpusEgress: {str(profile.needs_corpus_egress).lower()}")
        lines.append(f"    warmReplicas: {profile.warm_replicas}")
        lines.append("    tools:")
        for tool in sorted(profile.tools):
            lines.append(f"      - {tool}")
        lines.append("")

    generated = "\n".join(lines).rstrip() + "\n"

    # Appended to values.yaml rather than kept as a second file, so `helm
    # install` works without remembering to pass an extra -f.
    current = VALUES.read_text()
    if MARKER in current:
        current = current[: current.index(MARKER)].rstrip() + "\n"
    VALUES.write_text(current.rstrip() + "\n\n" + generated)
    print(f"wrote {VALUES.relative_to(REPO)} ({len(PROFILES)} profiles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
