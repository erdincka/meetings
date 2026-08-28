"""Cleaning up sandboxes.

Three mechanisms, because any one alone leaks pods:

1. Explicit release when a meeting finishes, driven by the lease table in
   ``SandboxManager.release_meeting``. Fast, and covers the normal case.
2. A startup sweep that deletes sandboxes labelled with a meeting that is no
   longer running. A backend killed mid-meeting never reaches step 1, and
   without this its sandboxes would sit warm and idle until something noticed.
3. A startup sweep for claims that were never labelled by this backend at
   all -- something exercising the Agent Sandbox SDK directly, outside the
   app. Mechanism 2 can't see these: it only ever looks at sandboxes it
   labelled itself, so an unlabelled claim is invisible to it and would
   otherwise sit forever.

The SandboxTemplate also carries a shutdown policy, so an orphan eventually
reaps itself even if the backend never comes back at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.sandbox_auth import MEETING_LABEL
from app.models.meetings import Meeting

logger = structlog.get_logger(__name__)

# Statuses that mean a meeting is still entitled to hold sandboxes.
LIVE_STATUSES = frozenset({"queued", "running", "stopping"})


async def reap_abandoned_meetings(session: AsyncSession) -> int:
    """Terminate meetings left running by a backend that is no longer here.

    A meeting is owned by the process executing it. If that process dies -- a
    restart, a crash, a cancelled WebSocket -- the meeting stays `queued` or
    `running` with nothing driving it. Because only one meeting may be active at
    a time, a single abandoned row then blocks *every* future meeting, and the
    only symptom is a 400 on create.

    Anything still marked live at startup is by definition abandoned: this
    process has just started and owns nothing yet.
    """
    from sqlalchemy import update

    result = await session.execute(
        update(Meeting)
        .where(Meeting.status.in_(sorted(LIVE_STATUSES)))
        .values(status="terminated", terminated=True)
        .returning(Meeting.id)
    )
    reaped = list(result.scalars().all())
    await session.commit()

    if reaped:
        logger.info(
            "abandoned_meetings_reaped",
            count=len(reaped),
            meeting_ids=[str(m) for m in reaped],
        )
    return len(reaped)


async def sweep_orphaned_sandboxes(session: AsyncSession) -> int:
    """Delete sandboxes belonging to meetings that are no longer running.

    Returns the number deleted. Never raises: a failed sweep must not stop the
    backend from starting.
    """
    try:
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except Exception:
            logger.info("sandbox_sweep_skipped_no_cluster_config")
            return 0

        api = client.CustomObjectsApi()
        listing = await api.list_namespaced_custom_object(
            group="agents.x-k8s.io",
            version="v1beta1",
            namespace=settings.SANDBOX_NAMESPACE,
            plural="sandboxes",
        )
    except Exception as exc:
        logger.warning("sandbox_sweep_failed", error=str(exc))
        return 0

    items = listing.get("items", [])
    if not items:
        return 0

    labelled = {
        item["metadata"]["name"]: (item["metadata"].get("labels") or {}).get(MEETING_LABEL)
        for item in items
    }
    meeting_ids = {m for m in labelled.values() if m}
    if not meeting_ids:
        return 0

    rows = await session.execute(
        select(Meeting.id, Meeting.status).where(Meeting.id.in_(meeting_ids))
    )
    live = {str(mid) for mid, status in rows.all() if status in LIVE_STATUSES}

    stale = [name for name, meeting in labelled.items() if meeting and meeting not in live]
    if not stale:
        return 0

    from kubernetes_asyncio import client as k8s

    api = k8s.CustomObjectsApi()
    deleted = 0
    for name in stale:
        try:
            await api.delete_namespaced_custom_object(
                group="agents.x-k8s.io",
                version="v1beta1",
                namespace=settings.SANDBOX_NAMESPACE,
                plural="sandboxes",
                name=name,
            )
            deleted += 1
        except Exception as exc:
            logger.warning("orphan_delete_failed", sandbox=name, error=str(exc))

    logger.info("orphaned_sandboxes_reaped", count=deleted, names=stale)
    return deleted


async def sweep_unlabeled_sandbox_claims(namespace: str | None = None) -> int:
    """Delete stale SandboxClaims that this backend never labelled.

    Every claim SandboxManager.acquire makes carries MEETING_LABEL from the
    moment it's created. One with no such label, past
    SANDBOX_UNLABELED_CLAIM_MAX_AGE_MINUTES, was made by something else
    entirely -- e.g. a script calling the Agent Sandbox SDK's
    ``create_sandbox`` directly -- and sweep_orphaned_sandboxes will never
    find it, since that sweep only matches on a label this process applies
    itself. Deletes the claim rather than the underlying Sandbox: the claim
    controls the Sandbox, so removing the Sandbox alone can just have it
    recreated to satisfy the claim.

    Never raises: a failed sweep must not stop the backend from starting.
    """
    try:
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except Exception:
            logger.info("unlabeled_claim_sweep_skipped_no_cluster_config")
            return 0

        api = client.CustomObjectsApi()
        listing = await api.list_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1beta1",
            namespace=namespace or settings.SANDBOX_NAMESPACE,
            plural="sandboxclaims",
        )
    except Exception as exc:
        logger.warning("unlabeled_claim_sweep_failed", error=str(exc))
        return 0

    max_age = timedelta(minutes=settings.SANDBOX_UNLABELED_CLAIM_MAX_AGE_MINUTES)
    now = datetime.now(UTC)

    stale = []
    for item in listing.get("items", []):
        labels = item["metadata"].get("labels") or {}
        if labels.get(MEETING_LABEL):
            continue
        created = item["metadata"].get("creationTimestamp")
        if not created:
            continue
        age = now - datetime.fromisoformat(created.replace("Z", "+00:00"))
        if age >= max_age:
            stale.append(item["metadata"]["name"])

    if not stale:
        return 0

    deleted = 0
    for name in stale:
        try:
            await api.delete_namespaced_custom_object(
                group="extensions.agents.x-k8s.io",
                version="v1beta1",
                namespace=namespace or settings.SANDBOX_NAMESPACE,
                plural="sandboxclaims",
                name=name,
            )
            deleted += 1
        except Exception as exc:
            logger.warning("unlabeled_claim_delete_failed", claim=name, error=str(exc))

    logger.info("unlabeled_sandbox_claims_reaped", count=deleted, names=stale)
    return deleted
