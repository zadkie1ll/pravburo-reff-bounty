from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from pravburo_ref_common.database import get_session
from pravburo_ref_common.models import Agent, AgentRole
from sqlalchemy.ext.asyncio import AsyncSession


async def require_agent(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Agent:
    agent_id = request.session.get("agent_id")
    agent = await session.get(Agent, agent_id) if agent_id else None
    if agent is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return agent


async def require_admin(agent: Annotated[Agent, Depends(require_agent)]) -> Agent:
    if agent.role != AgentRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return agent


CurrentAdmin = Annotated[Agent, Depends(require_admin)]
