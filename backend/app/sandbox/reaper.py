"""Cleaning up sandboxes.

Two mechanisms, because either alone leaks pods:

1. Explicit release when a meeting finishes. Fast, and covers the normal case.
2. A startup sweep that deletes sandboxes labelled with a meeting that is no
   longer running. A backend killed mid-meeting never reaches step 1, and
   without this its sandboxes would sit warm and idle until something noticed.

The SandboxTemplate also carries a shutdown policy, so an orphan eventually
reaps itself even if the backend never comes back at all.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.sandbox_auth import MEETING_LABEL
from app.models.meetings import Meeting

logger = structlog.get_logger(__name__)

# Statuses that mean a meeting is still entitled to hold sandboxes.
LIVE_STATUSES = frozenset({"queued", "running", "stopping"})


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


async def release_meeting_sandboxes(sandbox_names: list[str]) -> None:
    """Delete the sandboxes a finished meeting was using."""
    if not sandbox_names:
        return

    try:
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except Exception:
            return
        api = client.CustomObjectsApi()
    except Exception as exc:
        logger.warning("sandbox_release_unavailable", error=str(exc))
        return

    async def _delete(name: str) -> None:
        try:
            await api.delete_namespaced_custom_object(
                group="agents.x-k8s.io",
                version="v1beta1",
                namespace=settings.SANDBOX_NAMESPACE,
                plural="sandboxes",
                name=name,
            )
        except Exception as exc:
            logger.warning("sandbox_delete_failed", sandbox=name, error=str(exc))

    await asyncio.gather(*(_delete(n) for n in sandbox_names), return_exceptions=True)
    logger.info("meeting_sandboxes_released", count=len(sandbox_names))
