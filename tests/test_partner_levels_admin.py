import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any

from httpx import ASGITransport, AsyncClient
from pravburo_ref_common.database import engine, session_factory
from pravburo_ref_common.models import Agent, AgentRole, PartnerLevel, PartnerLevelMonth
from sqlalchemy import delete, select

from src.dependencies import require_admin
from src.main import app

FAKE_ADMIN = Agent(id=1, email="admin@example.com", role=AgentRole.ADMIN)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    async def with_dispose() -> None:
        try:
            await coro
        finally:
            await engine.dispose()

    asyncio.run(with_dispose())


def _csrf_from(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


async def _make_agent(marker: str) -> int:
    async with session_factory() as session:
        agent = Agent(email=f"{marker}@example.test", display_name=f"Партнёр{marker}")
        session.add(agent)
        await session.commit()
        return agent.id


async def _cleanup(agent_id: int) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(PartnerLevelMonth).where(PartnerLevelMonth.agent_id == agent_id)
        )
        await session.execute(delete(Agent).where(Agent.id == agent_id))
        await session.commit()


def test_partner_levels_search_finds_agent_by_name() -> None:
    async def scenario() -> None:
        marker = uuid.uuid4().hex[:8]
        agent_id = await _make_agent(marker)
        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(f"/admin/partner-levels?q=Партнёр{marker}")
            assert response.status_code == 200
            assert f"Партнёр{marker}" in response.text
            assert f"agent_id={agent_id}" in response.text
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(agent_id)

    _run(scenario())


def test_partner_levels_set_creates_manual_row() -> None:
    async def scenario() -> None:
        marker = uuid.uuid4().hex[:8]
        agent_id = await _make_agent(marker)
        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"/admin/partner-levels?agent_id={agent_id}")
                csrf = _csrf_from(page.text)
                response = await client.post(
                    "/admin/partner-levels/set",
                    data={
                        "agent_id": str(agent_id),
                        "year": "2026",
                        "month": "6",
                        "level": "pro",
                        "csrf": csrf,
                    },
                    follow_redirects=False,
                )
            assert response.status_code == 303

            async with session_factory() as session:
                row = await session.scalar(
                    select(PartnerLevelMonth).where(
                        PartnerLevelMonth.agent_id == agent_id,
                        PartnerLevelMonth.year == 2026,
                        PartnerLevelMonth.month == 6,
                    )
                )
                assert row is not None
                assert row.level == PartnerLevel.PRO
                assert row.is_manual is True
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(agent_id)

    _run(scenario())


def test_partner_levels_reset_to_auto_clears_manual_flag() -> None:
    async def scenario() -> None:
        marker = uuid.uuid4().hex[:8]
        agent_id = await _make_agent(marker)
        async with session_factory() as session:
            session.add(
                PartnerLevelMonth(
                    agent_id=agent_id,
                    year=2026,
                    month=5,
                    contracts_count=1,
                    level=PartnerLevel.START,
                    is_manual=True,
                )
            )
            await session.commit()

        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"/admin/partner-levels?agent_id={agent_id}")
                csrf = _csrf_from(page.text)
                response = await client.post(
                    "/admin/partner-levels/reset-to-auto",
                    data={
                        "agent_id": str(agent_id),
                        "year": "2026",
                        "month": "5",
                        "csrf": csrf,
                    },
                    follow_redirects=False,
                )
            assert response.status_code == 303

            async with session_factory() as session:
                row = await session.scalar(
                    select(PartnerLevelMonth).where(PartnerLevelMonth.agent_id == agent_id)
                )
                assert row.is_manual is False
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(agent_id)

    _run(scenario())


def test_rewards_page_lists_quarterly_bonus_without_application() -> None:
    from decimal import Decimal

    from pravburo_ref_common.models import Reward, RewardType

    async def scenario() -> None:
        marker = uuid.uuid4().hex[:8]
        agent_id = await _make_agent(marker)
        deal_id = f"quarterly:{agent_id}:2026Q3"
        async with session_factory() as session:
            session.add(
                Reward(
                    deal_id=deal_id,
                    application_id=None,
                    agent_id=agent_id,
                    reward_type=RewardType.QUARTERLY_BONUS,
                    amount=Decimal("15000.00"),
                )
            )
            await session.commit()

        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/admin/rewards")
            assert response.status_code == 200
            assert deal_id in response.text
            assert "Квартальный бонус" in response.text
        finally:
            app.dependency_overrides.pop(require_admin, None)
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.deal_id == deal_id))
                await session.commit()
            await _cleanup(agent_id)

    _run(scenario())
