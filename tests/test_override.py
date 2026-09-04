import asyncio
import uuid
from collections.abc import Coroutine
from decimal import Decimal
from typing import Any

from pravburo_ref_common.database import engine, session_factory
from pravburo_ref_common.models import (
    Agent,
    AgentCredential,
    NetworkOverrideRate,
    ReferralApplication,
    Reward,
    RewardType,
)
from sqlalchemy import delete, select

from src.service import create_reward_once


def _run(coro: Coroutine[Any, Any, None]) -> None:
    async def with_dispose() -> None:
        try:
            await coro
        finally:
            # Each test gets its own event loop via asyncio.run(); the shared
            # engine's pooled asyncpg connections are loop-bound, so dispose
            # here (inside this loop) rather than leaving them for the next test.
            await engine.dispose()

    asyncio.run(with_dispose())


class _Cleanup:
    def __init__(self) -> None:
        self.agent_ids: list[int] = []
        self.application_ids: list[int] = []
        self.deal_ids: list[str] = []

    async def run(self) -> None:
        async with session_factory() as session:
            for deal_id in self.deal_ids:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
            for application_id in self.application_ids:
                await session.execute(
                    delete(ReferralApplication).where(ReferralApplication.id == application_id)
                )
            # invited_by_agent_id points from a later agent to an earlier one;
            # delete newest-first so the FK is never left dangling mid-cleanup.
            for agent_id in reversed(self.agent_ids):
                await session.execute(delete(Agent).where(Agent.id == agent_id))
            await session.commit()


async def _rates() -> dict[int, Decimal]:
    async with session_factory() as session:
        rows = (await session.scalars(select(NetworkOverrideRate))).all()
        return {row.level: row.amount for row in rows}


async def _make_partner_chain(*, self_registered: list[bool]) -> tuple[list[int], _Cleanup]:
    """Build a chain of agents, each inviting the next (oldest first)."""
    cleanup = _Cleanup()
    agent_ids: list[int] = []
    async with session_factory() as session:
        previous_id: int | None = None
        for is_partner in self_registered:
            agent = Agent(email=f"{uuid.uuid4()}@example.test", invited_by_agent_id=previous_id)
            session.add(agent)
            await session.flush()
            if is_partner:
                session.add(AgentCredential(agent_id=agent.id, password_hash="x"))
            agent_ids.append(agent.id)
            previous_id = agent.id
        await session.commit()
    cleanup.agent_ids.extend(agent_ids)
    return agent_ids, cleanup


def test_partner_earner_pays_override_three_levels_up() -> None:
    async def scenario() -> None:
        # Оля -> Вася -> Олег -> Игорь, all partners (self-registered)
        agent_ids, cleanup = await _make_partner_chain(self_registered=[True, True, True, True])
        olya_id, vasya_id, oleg_id, igor_id = agent_ids
        async with session_factory() as session:
            application = ReferralApplication(
                agent_id=igor_id,
                full_name="Клиент Игоря",
                phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(application)
            await session.commit()
            cleanup.application_ids.append(application.id)
            application_id = application.id

        deal_id = str(uuid.uuid4())
        cleanup.deal_ids.append(deal_id)
        try:
            async with session_factory() as session:
                reward, created = await create_reward_once(
                    session,
                    deal_id,
                    application_id,
                    igor_id,
                    RewardType.ADVANCE,
                    Decimal("3000.00"),
                )
                assert created is True
                reward_id = reward.id

            rates = await _rates()
            async with session_factory() as session:
                overrides = (
                    await session.scalars(
                        select(Reward).where(Reward.source_reward_id == reward_id)
                    )
                ).all()
                by_agent = {o.agent_id: o for o in overrides}
                assert set(by_agent) == {oleg_id, vasya_id, olya_id}
                assert by_agent[oleg_id].network_level == 1
                assert by_agent[oleg_id].amount == rates[1]
                assert by_agent[vasya_id].network_level == 2
                assert by_agent[vasya_id].amount == rates[2]
                assert by_agent[olya_id].network_level == 3
                assert by_agent[olya_id].amount == rates[3]
        finally:
            await cleanup.run()

    _run(scenario())


def test_legacy_client_earner_pays_override_only_two_levels_up() -> None:
    async def scenario() -> None:
        # Оля -> Вася -> Олег, Олег is a legacy client (no credential/identity)
        agent_ids, cleanup = await _make_partner_chain(self_registered=[True, True, False])
        olya_id, vasya_id, oleg_id = agent_ids
        async with session_factory() as session:
            application = ReferralApplication(
                agent_id=oleg_id,
                full_name="Клиент",
                phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(application)
            await session.commit()
            cleanup.application_ids.append(application.id)
            application_id = application.id

        deal_id = str(uuid.uuid4())
        cleanup.deal_ids.append(deal_id)
        try:
            async with session_factory() as session:
                reward, created = await create_reward_once(
                    session, deal_id, application_id, oleg_id, RewardType.MAIN, Decimal("15000.00")
                )
                assert created is True
                reward_id = reward.id

            async with session_factory() as session:
                overrides = (
                    await session.scalars(
                        select(Reward).where(Reward.source_reward_id == reward_id)
                    )
                ).all()
                by_agent = {o.agent_id: o for o in overrides}
                assert set(by_agent) == {vasya_id, olya_id}
                assert by_agent[vasya_id].network_level == 1
                assert by_agent[olya_id].network_level == 2
        finally:
            await cleanup.run()

    _run(scenario())


def test_no_upline_creates_no_overrides() -> None:
    async def scenario() -> None:
        agent_ids, cleanup = await _make_partner_chain(self_registered=[True])
        (only_agent_id,) = agent_ids
        async with session_factory() as session:
            application = ReferralApplication(
                agent_id=only_agent_id,
                full_name="Клиент",
                phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(application)
            await session.commit()
            cleanup.application_ids.append(application.id)
            application_id = application.id

        deal_id = str(uuid.uuid4())
        cleanup.deal_ids.append(deal_id)
        try:
            async with session_factory() as session:
                reward, created = await create_reward_once(
                    session, deal_id, application_id, only_agent_id, RewardType.MAIN, Decimal("100")
                )
                assert created is True
                reward_id = reward.id

            async with session_factory() as session:
                overrides = (
                    await session.scalars(
                        select(Reward).where(Reward.source_reward_id == reward_id)
                    )
                ).all()
                assert overrides == []
        finally:
            await cleanup.run()

    _run(scenario())


def test_reward_without_amount_still_pays_the_flat_override() -> None:
    """Override is a flat sum per level now, not a percent of the source
    reward - it doesn't need the source reward to have an amount at all.
    """

    async def scenario() -> None:
        agent_ids, cleanup = await _make_partner_chain(self_registered=[True, True])
        olya_id, vasya_id = agent_ids
        async with session_factory() as session:
            application = ReferralApplication(
                agent_id=vasya_id,
                full_name="Клиент",
                phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
            )
            session.add(application)
            await session.commit()
            cleanup.application_ids.append(application.id)
            application_id = application.id

        deal_id = str(uuid.uuid4())
        cleanup.deal_ids.append(deal_id)
        try:
            async with session_factory() as session:
                reward, created = await create_reward_once(
                    session, deal_id, application_id, vasya_id, RewardType.MAIN
                )
                assert created is True
                reward_id = reward.id

            rates = await _rates()
            async with session_factory() as session:
                overrides = (
                    await session.scalars(
                        select(Reward).where(Reward.source_reward_id == reward_id)
                    )
                ).all()
                assert len(overrides) == 1
                assert overrides[0].agent_id == olya_id
                assert overrides[0].amount == rates[1]
        finally:
            await cleanup.run()

    _run(scenario())
