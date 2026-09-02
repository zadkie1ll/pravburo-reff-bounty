from decimal import Decimal

from pravburo_ref_common.models import ReferralApplication, Reward, RewardType
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


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
        await session.commit()
        await session.refresh(reward)
        return reward, True
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(Reward).where(Reward.deal_id == deal_id))
        if existing is None:
            raise
        return existing, False
