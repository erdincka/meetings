"""Tools available to a persona inside its sandbox.

Every tool here reaches the outside world through the backend's /internal API,
authenticated with the sandbox's projected ServiceAccount token. Sandboxes never
hold the application database credential.
"""

from .retrieval import build_retrieval_tool

TOOL_BUILDERS = {
    "retrieve_documents": build_retrieval_tool,
}

__all__ = ["TOOL_BUILDERS", "build_retrieval_tool"]
