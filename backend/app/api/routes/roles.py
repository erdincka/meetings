from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.domain.response import APIResponse
from app.domain.roles import RoleAgentCreate, RoleAgentResponse, RoleAgentUpdate
from app.models.roles import RoleAgent
from app.orchestration import profiles

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get("", response_model=APIResponse)
async def list_roles(session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(RoleAgent).order_by(RoleAgent.display_name)
    result = await session.execute(query)
    roles = result.scalars().all()

    return APIResponse(
        status="success", data=[RoleAgentResponse.model_validate(r).model_dump() for r in roles]
    )


@router.post("", response_model=APIResponse)
async def create_role(
    role_in: RoleAgentCreate, session: AsyncSession = Depends(get_db_session)
) -> APIResponse:
    new_role = RoleAgent(**role_in.model_dump())
    session.add(new_role)
    await session.commit()
    await session.refresh(new_role)

    logger.info("role_created", role_id=str(new_role.id))
    return APIResponse(
        status="success", data=RoleAgentResponse.model_validate(new_role).model_dump()
    )


@router.get("/capabilities/catalog", response_model=APIResponse)
async def capability_catalog() -> APIResponse:
    """The tool catalogue and the profiles that grant them.

    The persona editor needs this to show what a tool grant actually buys and
    which profile a selection resolves to. Resolution stays on the server so
    the rule has one home: a copy in TypeScript would drift from the one that
    decides what the cluster actually permits.
    """
    return APIResponse(
        status="success",
        data={
            "tools": [
                {"name": name, "guidance": profiles.TOOL_GUIDANCE.get(name, "")}
                for name in sorted(profiles.ALL_TOOLS)
            ],
            "profiles": [
                {
                    "name": p.name,
                    "description": p.description,
                    "tools": sorted(p.tools),
                    "can_execute_code": p.can_execute_code,
                    "holds_metrics_credential": p.needs_metrics_dsn,
                }
                for p in profiles.PROFILES
            ],
        },
    )


@router.post("/capabilities/resolve", response_model=APIResponse)
async def resolve_capability_profile(payload: dict[str, list[str]]) -> APIResponse:
    """Resolve a tool selection to the smallest profile that covers it.

    Returns the drift message rather than raising, so the editor can warn while
    the operator is still typing instead of failing at meeting start.
    """
    requested = payload.get("tools", [])
    try:
        profile = profiles.resolve(list(requested))
    except profiles.ProfileDriftError as exc:
        return APIResponse(
            status="success",
            data={"resolved": None, "in_profile": False, "reason": str(exc)},
        )

    return APIResponse(
        status="success",
        data={
            "resolved": profile.name,
            "description": profile.description,
            "in_profile": True,
            # The grant is the profile's whole tool set, not only what was
            # asked for -- worth showing, since it is what the pod really gets.
            "granted_tools": sorted(profile.tools),
            "can_execute_code": profile.can_execute_code,
            "holds_metrics_credential": profile.needs_metrics_dsn,
        },
    )


@router.get("/{role_id}", response_model=APIResponse)
async def get_role(role_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(RoleAgent).where(RoleAgent.id == role_id)
    result = await session.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    return APIResponse(status="success", data=RoleAgentResponse.model_validate(role).model_dump())


@router.put("/{role_id}", response_model=APIResponse)
async def update_role(
    role_id: UUID, role_in: RoleAgentUpdate, session: AsyncSession = Depends(get_db_session)
) -> APIResponse:
    query = select(RoleAgent).where(RoleAgent.id == role_id)
    result = await session.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    update_data = role_in.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(role, key, value)

    await session.commit()
    await session.refresh(role)
    return APIResponse(status="success", data=RoleAgentResponse.model_validate(role).model_dump())
