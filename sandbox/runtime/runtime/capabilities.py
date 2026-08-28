"""Capability enforcement inside the sandbox.

The backend sends a list of granted tools. That list is *intersected* with what
this sandbox's own capability file allows, so a compromised or buggy backend
cannot grant a tool the SandboxTemplate did not provision.

The file is mounted read-only from a ConfigMap referenced by the template, which
means changing it requires changing the template -- a Kubernetes-level action,
not an application-level one. This is the second of the five enforcement layers
described in docs/sandbox-security-model.md; the prompt is the weakest and RBAC
the strongest.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

CAPABILITY_FILE = Path(os.getenv("SANDBOX_CAPABILITY_FILE", "/etc/sandbox/capabilities"))

# The baseline every profile carries. Anything beyond this is granted by the
# profile's SandboxTemplate, not by this file.
BASELINE_TOOLS = frozenset({"retrieve_documents"})


def allowed_tools() -> frozenset[str]:
    """Tools this sandbox is provisioned for.

    Falls back to the baseline set when no capability file is mounted, which is
    the case for local development and the test suite. It deliberately does not
    fall back to "everything".
    """
    if not CAPABILITY_FILE.exists():
        logger.info("capability_file_absent_using_baseline", path=str(CAPABILITY_FILE))
        return BASELINE_TOOLS

    entries = {
        line.strip()
        for line in CAPABILITY_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    return frozenset(entries)


def resolve_grant(requested: list[str]) -> tuple[list[str], list[str]]:
    """Intersect a requested grant with what this sandbox actually allows.

    Returns ``(active, refused)``. Refusals are reported back to the backend
    rather than raised, so a mismatch is visible in the audit trail instead of
    failing the meeting outright.
    """
    allowed = allowed_tools()
    active = sorted(t for t in requested if t in allowed)
    refused = sorted(set(requested) - allowed)
    if refused:
        logger.warning("tool_grant_refused", refused=refused, allowed=sorted(allowed))
    return active, refused
