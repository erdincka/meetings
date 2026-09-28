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

Reaching the claimed sandbox is a second, narrower decision. The job is sent
through the Sandbox Router, which wants a token naming that one sandbox by UID;
this persona holds no signing key, so it asks the backend for one and the
backend issues it only for a claim carrying this persona's own identity. RBAC
decides whether a persona may execute code at all; the token decides that it may
talk to the sandbox it claimed and to nobody else's.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

EXEC_NAMESPACE = os.getenv("SANDBOX_EXEC_NAMESPACE", "meetings-exec")
EXEC_WARM_POOL = os.getenv("SANDBOX_EXEC_WARM_POOL", "exec-python")
EXEC_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_EXEC_TIMEOUT", "60"))
EXEC_PORT = int(os.getenv("SANDBOX_EXEC_PORT", "8080"))
# Set by the chart. Empty means the sandbox is addressed directly, which only
# works where no NetworkPolicy is in the way -- a runtime outside a cluster.
ROUTER_URL = os.getenv("SANDBOX_ROUTER_URL", "")

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
    client: Any = None,
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
            result = await _run_in_sandbox(handle, code, backend=client)
        except Exception as exc:
            # Reported like any other tool failure. Raising would end the turn
            # and show an outage where the transcript should show a reason.
            logger.error("exec_sandbox_call_failed", error=str(exc))
            return f"Code execution is unavailable: {exc}"
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

        identity = {
            "sandbox.users.io/meeting-id": meeting_id,
            "sandbox.users.io/agent-id": agent_id,
        }
        # Generic over the sandbox handle type since SDK 1.0; the default is used.
        client: Any = SandboxClient(connection_config=SandboxInClusterConnectionConfig())
        return client.create_sandbox(
            warmpool=EXEC_WARM_POOL,
            namespace=EXEC_NAMESPACE,
            pod_labels=identity,
            # On the claim as well as the pod. The backend issues a router
            # token only for a claim labelled with the caller's own identity,
            # and this Role cannot update or patch, so these cannot be rewritten
            # onto somebody else's claim afterwards.
            labels=identity,
            # The deadline, held by the controller. This persona releases the
            # sandbox when the job returns, but a persona that is evicted
            # mid-job never gets to, and nothing else knows the claim exists.
            shutdown_after_seconds=EXEC_TIMEOUT_SECONDS + 60,
        )

    try:
        return await asyncio.to_thread(_create)
    except Exception as exc:
        if _is_forbidden(exc):
            raise ExecutionDenied(str(exc)) from exc
        raise


async def _route(handle: Any, backend: Any) -> tuple[str, dict[str, str]]:
    """Where to send the job, and what to present when sending it."""
    if not ROUTER_URL or backend is None:
        return f"http://{handle.get_pod_ip()}:{EXEC_PORT}/run", {}
    response = await backend.post("/internal/v1/exec-token", json={"claim_name": handle.claim_name})
    response.raise_for_status()
    data = response.json()["data"]
    return data["url"], data["headers"]


async def _run_in_sandbox(handle: Any, code: str, backend: Any = None) -> dict[str, Any]:
    """Send the job to the exec sandbox and collect the result.

    Talks to the sandbox's own small job server rather than the SDK's file and
    command transport, which needs a helper this image does not carry. A plain
    HTTP call keeps the security boundary independent of SDK internals: the
    exec tier still has no egress, and the router is the only route in.
    """
    import httpx

    url, headers = await _route(handle, backend)
    async with httpx.AsyncClient(timeout=EXEC_TIMEOUT_SECONDS + 30) as client:
        response = await client.post(url, json={"code": code, "inputs": {}}, headers=headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
    return result


async def _release(handle: Any) -> None:
    import asyncio

    def _terminate() -> None:
        handle.terminate()

    try:
        await asyncio.to_thread(_terminate)
    except Exception as exc:
        logger.warning("exec_sandbox_release_failed", error=str(exc))
