"""Model-authored Python, executed in a second sandbox tier.

This is the tool the whole least-privilege story is built around.

When an agent calls it, its persona sandbox asks the Kubernetes apiserver to
create a SandboxClaim for the exec warm pool. Whether that succeeds is decided
by RBAC: only the ServiceAccount bound to the exec-sandbox-claimer Role may do
it. A persona in a profile without code execution gets a **403 from the
apiserver** -- not a refusal from our own code, not a prompt that politely
declines, but the cluster saying no.

That distinction is the point. Everything above this layer can be talked around
by a sufficiently persuasive prompt; this cannot.

The exec tier itself has no network whatsoever, so even code that runs is
reaching nothing.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

EXEC_NAMESPACE = os.getenv("SANDBOX_EXEC_NAMESPACE", "meetings-exec")
EXEC_WARM_POOL = os.getenv("SANDBOX_EXEC_WARM_POOL", "exec-python")
EXEC_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_EXEC_TIMEOUT", "60"))

# Recognisable in logs and in the audit matrix, so a denial reads as a policy
# decision rather than an unexplained failure.
DENIED_PREFIX = "DENIED_BY_CLUSTER"


class ExecutionDenied(RuntimeError):
    """The apiserver refused to let this persona claim an exec sandbox."""


def _is_forbidden(exc: Exception) -> bool:
    """True when the failure is an authorization decision rather than a fault."""
    status = getattr(exc, "status", None)
    if status == 403:
        return True
    text = str(exc).lower()
    return "forbidden" in text or "cannot create resource" in text or "403" in text


def build_code_exec_tool(
    *,
    agent_id: str,
    meeting_id: str,
    artifact_writer: Any,
    **_ignored: Any,
) -> StructuredTool:
    async def run_python_analysis(code: str, produce: str = "table") -> str:
        """Run Python to analyse data and produce a table or a chart.

        Write ordinary Python. pandas, numpy and matplotlib are available. To
        return a chart, save a PNG into /work/out/; to return a table, print it.
        There is no network access, so fetch nothing -- pass any data you need
        through the code itself.
        """
        started = time.monotonic()
        try:
            handle = await _claim_exec_sandbox(agent_id=agent_id, meeting_id=meeting_id)
        except ExecutionDenied as exc:
            # Reported, not raised: the agent should learn it lacks the
            # capability and carry on contributing, and the denial belongs in
            # the audit trail.
            logger.warning("code_execution_denied", agent_id=agent_id, error=str(exc))
            return (
                f"{DENIED_PREFIX}: this persona is not permitted to execute code. "
                "The Kubernetes API server refused the sandbox claim. Continue "
                "without running analysis, or ask a colleague whose role covers it."
            )
        except Exception as exc:
            logger.error("exec_sandbox_unavailable", error=str(exc))
            return f"Code execution is unavailable: {exc}"

        try:
            result = await _run_in_sandbox(handle, code)
        finally:
            await _release(handle)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not result.get("ok"):
            return f"The analysis failed after {elapsed_ms}ms:\n{result.get('error')}"

        summary = [result.get("stdout", "").strip() or "(no output)"]
        for filename in result.get("files", []):
            artifact_id = await artifact_writer(
                kind="chart" if filename.endswith(".png") else "table",
                title=filename,
                body=result["artifacts"][filename],
                mime_type="image/png" if filename.endswith(".png") else "text/plain",
            )
            summary.append(f"[artifact {artifact_id}: {filename}]")
        return "\n".join(summary)

    return StructuredTool.from_function(
        coroutine=run_python_analysis,
        name="run_python_analysis",
        description=run_python_analysis.__doc__ or "",
    )


async def _claim_exec_sandbox(*, agent_id: str, meeting_id: str) -> Any:
    """Claim an exec sandbox, translating an authorization refusal.

    Runs the synchronous SDK in a worker thread so the runtime's event loop
    keeps serving its SSE stream.
    """
    import asyncio

    def _create() -> Any:
        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

        client = SandboxClient(connection_config=SandboxInClusterConnectionConfig())
        return client.create_sandbox(
            warmpool=EXEC_WARM_POOL,
            namespace=EXEC_NAMESPACE,
            pod_labels={
                "sandbox.users.io/meeting-id": meeting_id,
                "sandbox.users.io/agent-id": agent_id,
            },
        )

    try:
        return await asyncio.to_thread(_create)
    except Exception as exc:
        if _is_forbidden(exc):
            raise ExecutionDenied(str(exc)) from exc
        raise


async def _run_in_sandbox(handle: Any, code: str) -> dict[str, Any]:
    """Write the job, run it, and collect whatever it produced."""
    import asyncio
    import base64

    def _execute() -> dict[str, Any]:
        handle.files.write("/work/in/job.json", json.dumps({"code": code, "inputs": {}}))
        handle.commands.run(
            f"timeout {EXEC_TIMEOUT_SECONDS} python /opt/runner.py",
        )
        raw = handle.files.read("/work/out/result.json")
        result: dict[str, Any] = json.loads(raw)

        # Binary output is base64'd on the way back; text is passed through.
        artifacts: dict[str, str] = {}
        for filename in result.get("files", []):
            content = handle.files.read(f"/work/out/{filename}")
            artifacts[filename] = (
                base64.b64encode(content).decode() if isinstance(content, bytes) else str(content)
            )
        result["artifacts"] = artifacts
        return result

    return await asyncio.to_thread(_execute)


async def _release(handle: Any) -> None:
    import asyncio

    def _terminate() -> None:
        handle.terminate()

    try:
        await asyncio.to_thread(_terminate)
    except Exception as exc:
        logger.warning("exec_sandbox_release_failed", error=str(exc))
