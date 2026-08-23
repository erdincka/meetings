"""Tools available to a persona inside its sandbox.

Registration is the *weakest* of the five enforcement layers. A tool appearing
here means the runtime is willing to offer it; whether the agent can actually
use it is decided further down -- by the capability file this sandbox mounts, by
the Secrets its template references, by its NetworkPolicy, and ultimately by
what RBAC lets it ask the apiserver for.

Building a tool must therefore never itself grant access. `query_business_metrics`
constructs happily in a sandbox with no DSN mounted; it simply reports that it
has no credential. `run_python_analysis` constructs happily without permission to
claim an exec sandbox; the apiserver refuses at call time. Both failures are
honest and visible, which is what makes the audit trail worth reading.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from .artifacts import build_artifact_tools
from .code_exec import build_code_exec_tool
from .metrics_sql import build_metrics_tool
from .policy import build_policy_tool
from .retrieval import build_retrieval_tool

# Tools that return a single instance.
_SINGLE: dict[str, Callable[..., StructuredTool]] = {
    "retrieve_documents": build_retrieval_tool,
    "query_business_metrics": build_metrics_tool,
    "run_python_analysis": build_code_exec_tool,
    "check_policy_compliance": build_policy_tool,
}

# Artifact tools are built together because they share a client and a scope.
_ARTIFACT_TOOLS = frozenset({"draft_artifact", "read_artifact", "record_action_item"})

KNOWN_TOOLS = frozenset(_SINGLE) | _ARTIFACT_TOOLS


def build_tools(names: list[str], **context: Any) -> list[BaseTool]:
    """Construct the granted tools, skipping any this runtime cannot build.

    An unknown name is skipped rather than raised: the backend and the runtime
    are versioned separately, and a newer backend granting a tool an older image
    has never heard of should degrade to fewer tools, not a failed meeting.
    """
    tools: list[BaseTool] = []
    wanted = set(names)

    # Builders are handed the whole context and take what they need. The
    # convention is enforced by tests/test_tool_registry.py, which builds every
    # known tool from one context dict.

    for name in sorted(wanted & set(_SINGLE)):
        tools.append(_SINGLE[name](**context))

    if wanted & _ARTIFACT_TOOLS:
        tools.extend(tool for tool in build_artifact_tools(**context) if tool.name in wanted)

    return tools


__all__ = ["KNOWN_TOOLS", "build_tools"]
