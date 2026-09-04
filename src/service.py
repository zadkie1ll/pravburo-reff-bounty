from decimal import Decimal

from pravburo_ref_common.models import (
    Agent,
    AgentCredential,
    AgentIdentity,
    NetworkOverrideRate,
    ReferralApplication,
    Reward,
    RewardType,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _max_override_levels(session: AsyncSession, agent_id: int) -> int:
    """A consciously self-registered partner earns override 3 levels up;
    an agent whose account only exists because they're a bankruptcy client
    (auto-created, never registered themselves) earns it just 2 levels up.
    """
    has_credential = (
        await session.scalar(
            select(AgentCredential.agent_id).where(AgentCredential.agent_id == agent_id)
        )
        is not None
    )
    has_identity = (
        await session.scalar(
            select(AgentIdentity.agent_id).where(AgentIdentity.agent_id == agent_id)
        )
        is not None
    )
    return 3 if has_credential or has_identity else 2


async def _build_override_rewards(session: AsyncSession, source: Reward) -> list[Reward]:
    max_levels = await _max_override_levels(session, source.agent_id)
    rate_rows = (await session.scalars(select(NetworkOverrideRate))).all()
    rates = {rate.level: rate.amount for rate in rate_rows}

    overrides: list[Reward] = []
    current_agent = await session.get(Agent, source.agent_id)
    for level in range(1, max_levels + 1):
        if current_agent is None or current_agent.invited_by_agent_id is None:
            break
        upline = await session.get(Agent, current_agent.invited_by_agent_id)
        if upline is None:
            break
        amount = rates.get(level)
        overrides.append(
            Reward(
                deal_id=source.deal_id,
                application_id=source.application_id,
                agent_id=upline.id,
                reward_type=RewardType.OVERRIDE,
                amount=amount,
                network_level=level,
                source_reward_id=source.id,
            )
        )
        current_agent = upline
    return overrides


async def create_reward_once(
    session: AsyncSession,
    deal_id: str,
    application_id: int,
    agent_id: int,
    reward_type: RewardType = RewardType.MAIN,
    amount: Decimal | None = None,
) -> tuple[Reward, bool]:
    application = await session.get(ReferralApplication, application_id)
    if application is None or application.agent_id != agent_id:
        raise ValueError("Referral attribution not found")
    reward = Reward(
        deal_id=deal_id,
        application_id=application_id,
        agent_id=agent_id,
        reward_type=reward_type,
        amount=amount,
    )
    session.add(reward)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Reward).where(
                Reward.deal_id == deal_id,
                Reward.reward_type == reward_type,
                Reward.agent_id == agent_id,
            )
        )
        if existing is None:
            raise
        return existing, False

    if reward_type != RewardType.OVERRIDE:
        for override in await _build_override_rewards(session, reward):
            session.add(override)

    await session.commit()
    await session.refresh(reward)
    return reward, True
