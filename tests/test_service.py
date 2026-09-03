import asyncio
import uuid
from decimal import Decimal

from pravburo_ref_common.database import session_factory
from pravburo_ref_common.models import Agent, ReferralApplication, Reward, RewardType
from sqlalchemy import delete

from src.service import create_reward_once


def test_create_reward_once_is_idempotent_per_deal_and_type_and_agent() -> None:
    """A retried webhook delivery (new request, new session) must not double-pay.

    Mirrors production: each call to create_reward_once gets its own session via
    Depends(get_session), so idempotency has to hold across sessions, not just
    within one - that's what the deal_id/reward_type/agent_id unique constraint
    (and the fallback lookup in create_reward_once) actually has to protect.
    """

    async def scenario() -> None:
        async with session_factory() as session:
            agent = Agent(
                email=f"{uuid.uuid4()}@example.test",
                phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(agent)
            await session.flush()
            application = ReferralApplication(
                agent_id=agent.id,
                full_name="Тест Тестов",
                phone_normalized=f"+7998{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(application)
            await session.commit()
            agent_id, application_id = agent.id, application.id

        deal_id = str(uuid.uuid4())
        try:
            async with session_factory() as session:
                reward, created = await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.ADVANCE
                )
            assert created is True

            async with session_factory() as session:
                retried, created_again = await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.ADVANCE
                )
            assert created_again is False
            assert retried.id == reward.id

            async with session_factory() as session:
                main_reward, main_created = await create_reward_once(
                    session,
                    deal_id,
                    application_id,
                    agent_id,
                    RewardType.MAIN,
                    Decimal("15000.00"),
                )
            assert main_created is True
            assert main_reward.id != reward.id
        finally:
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
                await session.execute(
                    delete(ReferralApplication).where(ReferralApplication.id == application_id)
                )
                await session.execute(delete(Agent).where(Agent.id == agent_id))
                await session.commit()

    asyncio.run(scenario())
