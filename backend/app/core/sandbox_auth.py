"""Authenticating persona sandboxes.

A sandbox presents its projected ServiceAccount token. The backend validates it
with a Kubernetes TokenReview and then derives *which persona is calling* from
the sandbox's own pod labels -- not from anything the request body says.

That distinction is the whole point. The model inside the sandbox composes the
request body; if the caller's identity came from there, a prompt-injected agent
could read another persona's private library simply by asking. Labels are set by
the backend at sandbox creation and are not reachable from inside the container.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import Header, HTTPException

from app.core.config import settings

logger = structlog.get_logger(__name__)

MEETING_LABEL = "meetings.io/meeting-id"
AGENT_LABEL = "meetings.io/agent-id"
PROFILE_LABEL = "meetings.io/profile"


@dataclass(frozen=True)
class SandboxIdentity:
    """A verified caller. All fields come from the cluster, never the request."""

    service_account: str
    namespace: str
    pod_name: str
    agent_id: str | None
    meeting_id: str | None
    profile: str | None


class SandboxAuthenticator:
    """Validates sandbox tokens and resolves the calling persona.

    Kept behind a class so tests can substitute a fake without patching module
    globals, and so the Kubernetes client is created lazily -- the backend must
    start and serve /health even where it has no cluster access at all.
    """

    def __init__(self) -> None:
        self._api: object | None = None

    async def _load_api(self) -> object:
        if self._api is None:
            from kubernetes_asyncio import client, config

            try:
                config.load_incluster_config()
            except Exception:
                await config.load_kube_config()
            self._api = client
        return self._api

    async def authenticate(self, token: str) -> SandboxIdentity:
        from kubernetes_asyncio import client as k8s_client

        await self._load_api()
        api = k8s_client.AuthenticationV1Api()
        review = k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token))

        result = await api.create_token_review(review)
        status = result.status
        if not status or not status.authenticated:
            raise HTTPException(status_code=401, detail="Sandbox token rejected")

        username = status.user.username or ""
        # system:serviceaccount:<namespace>:<name>
        parts = username.split(":")
        if len(parts) != 4 or parts[0] != "system" or parts[1] != "serviceaccount":
            raise HTTPException(status_code=403, detail="Not a ServiceAccount token")
        namespace, service_account = parts[2], parts[3]

        if namespace not in (settings.SANDBOX_NAMESPACE, settings.SANDBOX_EXEC_NAMESPACE):
            raise HTTPException(status_code=403, detail="Token is not from a sandbox namespace")

        pod_name = ""
        extra = getattr(status.user, "extra", None) or {}
        for key in ("authentication.kubernetes.io/pod-name",):
            value = extra.get(key)
            if value:
                pod_name = value[0] if isinstance(value, list) else str(value)

        labels = await self._pod_labels(namespace, pod_name) if pod_name else {}

        return SandboxIdentity(
            service_account=service_account,
            namespace=namespace,
            pod_name=pod_name,
            agent_id=labels.get(AGENT_LABEL),
            meeting_id=labels.get(MEETING_LABEL),
            profile=labels.get(PROFILE_LABEL),
        )

    async def _pod_labels(self, namespace: str, pod_name: str) -> dict[str, str]:
        from kubernetes_asyncio import client as k8s_client

        try:
            core = k8s_client.CoreV1Api()
            pod = await core.read_namespaced_pod(name=pod_name, namespace=namespace)
            return dict(pod.metadata.labels or {})
        except Exception as exc:
            logger.warning("pod_label_lookup_failed", pod=pod_name, error=str(exc))
            return {}


authenticator = SandboxAuthenticator()


async def require_sandbox_identity(
    authorization: str = Header(default=""),
) -> SandboxIdentity:
    """FastAPI dependency: a verified sandbox caller, or 401/403."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing sandbox token")
    return await authenticator.authenticate(authorization.removeprefix("Bearer ").strip())
