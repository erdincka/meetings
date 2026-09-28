"""Reading sandbox objects back from the cluster.

Small, and separate from the manager: these are lookups made on behalf of a
caller the backend has already authenticated, not part of claiming or releasing.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CLAIM_GROUP = "extensions.agents.x-k8s.io"
SANDBOX_GROUP = "agents.x-k8s.io"
VERSION = "v1beta1"


async def _load() -> Any:
    from kubernetes_asyncio import client, config

    try:
        config.load_incluster_config()
    except Exception:
        await config.load_kube_config()
    return client


async def get_claim(namespace: str, name: str) -> dict[str, Any]:
    client = await _load()
    obj: dict[str, Any] = await client.CustomObjectsApi().get_namespaced_custom_object(
        group=CLAIM_GROUP, version=VERSION, namespace=namespace, plural="sandboxclaims", name=name
    )
    return obj


async def get_sandbox(namespace: str, name: str) -> dict[str, Any]:
    client = await _load()
    obj: dict[str, Any] = await client.CustomObjectsApi().get_namespaced_custom_object(
        group=SANDBOX_GROUP, version=VERSION, namespace=namespace, plural="sandboxes", name=name
    )
    return obj


async def lifecycle_events(namespace: str, names: set[str]) -> list[dict[str, Any]]:
    """Kubernetes Events about the named Sandboxes, SandboxClaims and Pods.

    These are the controller's own account of what it did -- adopted a warm
    sandbox, created a pod, expired a claim -- and are the only record of a
    sandbox's life that the application did not write itself.

    They are not an audit trail and must not be presented as one. The apiserver
    keeps an Event for about an hour, nothing signs it, and it names objects
    rather than the persona that was running in them; the join to a persona is
    made by the caller, from the leases it holds.
    """
    if not names:
        return []
    client = await _load()
    # core/v1, not events.k8s.io/v1. They are the same objects, but the
    # generated events.k8s.io model insists on eventTime, which events written
    # through the older API do not carry -- one such event in the namespace
    # and the whole list raises instead of returning.
    listing = await client.CoreV1Api().list_namespaced_event(namespace=namespace)
    out: list[dict[str, Any]] = []
    for ev in listing.items:
        regarding = ev.involved_object
        if regarding is None or regarding.name not in names:
            continue
        if regarding.kind not in ("Sandbox", "SandboxClaim", "Pod"):
            continue
        when = ev.event_time or ev.last_timestamp or ev.metadata.creation_timestamp
        source = ev.source.component if ev.source else None
        out.append(
            {
                "time": when.isoformat() if when else None,
                "namespace": namespace,
                "kind": regarding.kind,
                "name": regarding.name,
                "uid": regarding.uid,
                "type": ev.type,
                "reason": ev.reason,
                "action": ev.action,
                "note": ev.message,
                "reported_by": ev.reporting_component or source,
            }
        )
    return sorted(out, key=lambda e: e["time"] or "")
