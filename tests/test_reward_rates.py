import asyncio
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from pravburo_ref_common.database import engine, session_factory
from pravburo_ref_common.models import Agent, AgentRole, RewardStageRate, RewardType

from src.dependencies import require_admin
from src.main import app

FAKE_ADMIN = Agent(id=1, email="admin@example.com", role=AgentRole.ADMIN)


def _run(coro) -> None:
    async def with_dispose() -> None:
        try:
            await coro
        finally:
            await engine.dispose()

    asyncio.run(with_dispose())


def _csrf_from(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def test_reward_rates_page_shows_configured_amounts() -> None:
    async def scenario() -> None:
        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/admin/reward-rates")
        finally:
            app.dependency_overrides.pop(require_admin, None)

        assert response.status_code == 200
        assert "Аванс" in response.text
        assert "Основная выплата" in response.text

    _run(scenario())


def test_reward_rates_submit_updates_amounts() -> None:
    async def scenario() -> None:
        app.dependency_overrides[require_admin] = lambda: FAKE_ADMIN
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/admin/reward-rates")
                csrf = _csrf_from(page.text)
                response = await client.post(
                    "/admin/reward-rates",
                    data={"amount_advance": "3500", "amount_main": "12000", "csrf": csrf},
                    follow_redirects=False,
                )
            assert response.status_code == 303

            async with session_factory() as session:
                advance = await session.get(RewardStageRate, RewardType.ADVANCE)
                main = await session.get(RewardStageRate, RewardType.MAIN)
                assert advance.amount == Decimal("3500.00")
                assert main.amount == Decimal("12000.00")
        finally:
            app.dependency_overrides.pop(require_admin, None)
            async with session_factory() as session:
                advance = await session.get(RewardStageRate, RewardType.ADVANCE)
                main = await session.get(RewardStageRate, RewardType.MAIN)
                advance.amount = Decimal("3000.00")
                main.amount = Decimal("10000.00")
                await session.commit()

    _run(scenario())
