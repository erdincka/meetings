"""Persona sandbox lifecycle.

Sandboxes are claimed from a warm pool on first use, reused for the rest of the
meeting, and terminated when it ends.

Acquisition is lazy -- on the supervisor's first selection of an attendee, not
at meeting start -- because a five-person meeting where two people never speak
should not hold five pods. A warm claim costs a few hundred milliseconds against
a multi-second model turn; a *cold* gVisor pod start costs seconds, which is
exactly why the warm pool exists and why no turn may ever pay for one.

Nothing durable lives in a sandbox. If one dies mid-meeting the backend claims
another, replays the persona bind, and re-issues the turn; the turn_results
table makes that safe to do.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from app.core.config import settings
from app.core.sandbox_auth import AGENT_LABEL, MEETING_LABEL, PROFILE_LABEL

logger = structlog.get_logger(__name__)

DEFAULT_WARM_POOL = "persona-baseline"
RUNTIME_PORT = 8080


@dataclass(frozen=True)
class SandboxHandle:
    """A claimed sandbox and how to reach it."""

    claim_name: str
    sandbox_name: str
    namespace: str
    base_url: str


class SandboxUnavailableError(RuntimeError):
    """Raised when a sandbox could not be claimed or did not become ready."""


class SandboxManager:
    """Claims sandboxes from warm pools and resolves their addresses.

    The Agent Sandbox Python SDK is synchronous, so its calls run in a worker
    thread rather than blocking the event loop that is concurrently streaming
    meeting events to the browser.
    """

    def __init__(self, namespace: str | None = None) -> None:
        self.namespace = namespace or settings.SANDBOX_NAMESPACE
        self._client: object | None = None
        self._lock = asyncio.Lock()

    def _sdk_client(self) -> object:
        if self._client is None:
            from k8s_agent_sandbox import SandboxClient
            from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

            # In-cluster: the backend talks to sandboxes over cluster DNS. The
            # Sandbox Router exists for callers outside the cluster, where
            # port-forward is unusable because it is incompatible with gVisor.
            self._client = SandboxClient(
                connection_config=SandboxInClusterConnectionConfig(),
            )
        return self._client

    async def acquire(
        self,
        *,
        meeting_id: str,
        agent_id: str,
        profile: str = "baseline",
        warm_pool: str = DEFAULT_WARM_POOL,
        ready_timeout: int = 180,
    ) -> SandboxHandle:
        """Claim a sandbox and return how to reach it.

        Labels are applied to the pod here, at creation. They are what
        sandbox_auth reads back to decide which persona a caller is, so they
        must never be derived from anything inside the sandbox.
        """
        pod_labels = {
            MEETING_LABEL: meeting_id,
            AGENT_LABEL: agent_id,
            PROFILE_LABEL: profile,
        }

        def _create() -> tuple[str, str]:
            client = self._sdk_client()
            sandbox = client.create_sandbox(  # type: ignore[attr-defined]
                warmpool=warm_pool,
                namespace=self.namespace,
                sandbox_ready_timeout=ready_timeout,
                pod_labels=pod_labels,
                labels=pod_labels,
            )
            return sandbox.claim_name, sandbox.sandbox_id

        async with self._lock:
            try:
                claim_name, sandbox_name = await asyncio.to_thread(_create)
            except Exception as exc:
                logger.error(
                    "sandbox_claim_failed",
                    meeting_id=meeting_id,
                    agent_id=agent_id,
                    warm_pool=warm_pool,
                    error=str(exc),
                )
                raise SandboxUnavailableError(str(exc)) from exc

        handle = SandboxHandle(
            claim_name=claim_name,
            sandbox_name=sandbox_name,
            namespace=self.namespace,
            base_url=self.base_url_for(sandbox_name),
        )
        logger.info(
            "sandbox_acquired",
            meeting_id=meeting_id,
            agent_id=agent_id,
            sandbox=sandbox_name,
            url=handle.base_url,
        )
        return handle

    def base_url_for(self, sandbox_name: str) -> str:
        """Cluster DNS address of a sandbox's Service.

        The controller publishes one per sandbox when the SandboxTemplate sets
        `service: true`, and reports it as status.serviceFQDN. It is constructed
        here rather than read back to avoid an extra API round trip on the hot
        path; the shape is verified by the smoke-sandbox gate.
        """
        return f"http://{sandbox_name}.{self.namespace}.svc.cluster.local:{RUNTIME_PORT}"

    async def release(self, handle: SandboxHandle) -> None:
        """Terminate a claimed sandbox. Never raises: cleanup must not fail a meeting."""

        def _delete() -> None:
            client = self._sdk_client()
            client.delete_sandbox(  # type: ignore[attr-defined]
                claim_name=handle.claim_name, namespace=handle.namespace
            )

        try:
            await asyncio.to_thread(_delete)
            logger.info("sandbox_released", sandbox=handle.sandbox_name)
        except Exception as exc:
            logger.warning("sandbox_release_failed", sandbox=handle.sandbox_name, error=str(exc))

    async def release_all(self, handles: list[SandboxHandle]) -> None:
        await asyncio.gather(*(self.release(h) for h in handles), return_exceptions=True)


manager = SandboxManager()
