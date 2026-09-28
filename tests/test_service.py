import asyncio
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from pravburo_ref_common.database import engine, session_factory
from pravburo_ref_common.models import (
    Agent,
    PartnerLevel,
    PartnerLevelMonth,
    ReferralApplication,
    Reward,
    RewardStageRate,
    RewardType,
)
from sqlalchemy import delete

from src.service import create_reward_once


def _run(coro) -> None:
    async def with_dispose() -> None:
        try:
            await coro
        finally:
            # Loop-bound pooled connections must be disposed inside the same
            # loop that created them, before asyncio.run() closes it - see
            # test_override.py's identical helper for the full rationale.
            await engine.dispose()

    asyncio.run(with_dispose())


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

    _run(scenario())


def test_advance_amount_comes_from_configured_stage_rate_regardless_of_level() -> None:
    """ADVANCE is a flat sum - the single source of truth is RewardStageRate,
    admin-editable at /admin/reward-rates - any amount the caller passes is
    ignored and overridden by the configured rate.
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
                advance, _ = await create_reward_once(
                    session,
                    deal_id,
                    application_id,
                    agent_id,
                    RewardType.ADVANCE,
                    Decimal("999999.00"),
                )
            assert advance.amount == Decimal("3000.00")
        finally:
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
                await session.execute(
                    delete(ReferralApplication).where(ReferralApplication.id == application_id)
                )
                await session.execute(delete(Agent).where(Agent.id == agent_id))
                await session.commit()

    _run(scenario())


def test_main_amount_is_null_while_its_origin_month_is_still_open() -> None:
    """The main payout depends on the partner's FINAL level for the month
    their contract was signed - unknowable until pravburo-reff-site's
    run_monthly_level_close fixes it on the 1st of the next month. Until
    then, MAIN is created with amount=NULL rather than guessing.
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
                await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.ADVANCE
                )

            async with session_factory() as session:
                main, _ = await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.MAIN
                )
            assert main.amount is None
        finally:
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
                await session.execute(
                    delete(ReferralApplication).where(ReferralApplication.id == application_id)
                )
                await session.execute(delete(Agent).where(Agent.id == agent_id))
                await session.commit()

    _run(scenario())


def test_main_amount_uses_origin_months_fixed_level_once_closed() -> None:
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
                advance, _ = await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.ADVANCE
                )
                origin_month = advance.created_at.astimezone(ZoneInfo("Europe/Moscow"))

            async with session_factory() as session:
                session.add(
                    PartnerLevelMonth(
                        agent_id=agent_id,
                        year=origin_month.year,
                        month=origin_month.month,
                        contracts_count=4,
                        level=PartnerLevel.PRO,
                    )
                )
                await session.commit()

            async with session_factory() as session:
                main, _ = await create_reward_once(
                    session, deal_id, application_id, agent_id, RewardType.MAIN
                )
                pro_rate = await session.get(RewardStageRate, (RewardType.MAIN, PartnerLevel.PRO))
            assert main.amount == pro_rate.amount
        finally:
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
                await session.execute(
                    delete(PartnerLevelMonth).where(PartnerLevelMonth.agent_id == agent_id)
                )
                await session.execute(
                    delete(ReferralApplication).where(ReferralApplication.id == application_id)
                )
                await session.execute(delete(Agent).where(Agent.id == agent_id))
                await session.commit()

    _run(scenario())
