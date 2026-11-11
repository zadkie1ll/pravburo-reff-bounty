import asyncio
import uuid

from httpx import ASGITransport, AsyncClient
from pravburo_ref_common.database import engine, session_factory
from pravburo_ref_common.models import (
    Agent,
    AgentRole,
    ReferralApplication,
    Reward,
    RewardStatus,
    RewardType,
)
from sqlalchemy import delete

from src.dependencies import require_admin
from src.main import app
from src.routes import REJECTION_REASONS


def _run(coro) -> None:
    async def with_dispose() -> None:
        try:
            await coro
        finally:
            await engine.dispose()

    asyncio.run(with_dispose())


def _csrf_from(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


async def _make_pending_reward() -> tuple[Agent, int, int, int]:
    async with session_factory() as session:
        admin = Agent(email=f"{uuid.uuid4()}@example.test", role=AgentRole.ADMIN)
        partner = Agent(email=f"{uuid.uuid4()}@example.test", display_name="Партнёр")
        session.add_all([admin, partner])
        await session.flush()
        application = ReferralApplication(
            agent_id=partner.id,
            full_name="Клиент",
            phone_normalized=f"+7999{uuid.uuid4().int % 10**7:07d}",
        )
        session.add(application)
        await session.flush()
        reward = Reward(
            deal_id=str(uuid.uuid4()),
            application_id=application.id,
            agent_id=partner.id,
            reward_type=RewardType.ADVANCE,
            amount=3000,
        )
        session.add(reward)
        await session.commit()
        return admin, partner.id, application.id, reward.id


async def _cleanup(admin_id: int, partner_id: int, application_id: int, reward_id: int) -> None:
    async with session_factory() as session:
        await session.execute(delete(Reward).where(Reward.id == reward_id))
        await session.execute(
            delete(ReferralApplication).where(ReferralApplication.id == application_id)
        )
        await session.execute(delete(Agent).where(Agent.id.in_([admin_id, partner_id])))
        await session.commit()


def test_rejection_reasons_are_offered_as_a_list() -> None:
    async def scenario() -> None:
        admin, partner_id, application_id, reward_id = await _make_pending_reward()
        app.dependency_overrides[require_admin] = lambda: admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/admin/rewards")
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(admin.id, partner_id, application_id, reward_id)

        assert page.status_code == 200
        assert "<textarea" not in page.text
        for reason in REJECTION_REASONS:
            assert reason in page.text

    _run(scenario())


def test_reject_accepts_only_reason_from_the_list() -> None:
    async def scenario() -> None:
        admin, partner_id, application_id, reward_id = await _make_pending_reward()
        app.dependency_overrides[require_admin] = lambda: admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                csrf = _csrf_from((await client.get("/admin/rewards")).text)
                url = f"/admin/rewards/{reward_id}/decide"

                # Свободный текст и пустая причина не принимаются: начисление остаётся на решении.
                for bad_reason in ("Придуманная причина", ""):
                    await client.post(
                        url,
                        data={"decision": "reject", "reason": bad_reason, "csrf": csrf},
                        follow_redirects=False,
                    )
                    async with session_factory() as session:
                        reward = await session.get(Reward, reward_id)
                        assert reward.status == RewardStatus.PENDING
                        assert reward.rejection_reason is None

                await client.post(
                    url,
                    data={"decision": "reject", "reason": REJECTION_REASONS[1], "csrf": csrf},
                    follow_redirects=False,
                )
                async with session_factory() as session:
                    reward = await session.get(Reward, reward_id)
                    assert reward.status == RewardStatus.REJECTED
                    assert reward.rejection_reason == REJECTION_REASONS[1]
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(admin.id, partner_id, application_id, reward_id)

    _run(scenario())


def test_approve_does_not_need_a_reason() -> None:
    async def scenario() -> None:
        admin, partner_id, application_id, reward_id = await _make_pending_reward()
        app.dependency_overrides[require_admin] = lambda: admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                csrf = _csrf_from((await client.get("/admin/rewards")).text)
                await client.post(
                    f"/admin/rewards/{reward_id}/decide",
                    data={"decision": "approve", "reason": "", "csrf": csrf},
                    follow_redirects=False,
                )
                async with session_factory() as session:
                    reward = await session.get(Reward, reward_id)
                    assert reward.status == RewardStatus.APPROVED
        finally:
            app.dependency_overrides.pop(require_admin, None)
            await _cleanup(admin.id, partner_id, application_id, reward_id)

    _run(scenario())


def test_rewards_pagination_is_bounded_and_does_not_repeat_rows() -> None:
    import re

    async def scenario() -> None:
        admin, partner_id, application_id, reward_id = await _make_pending_reward()
        app.dependency_overrides[require_admin] = lambda: admin
        try:
            async with session_factory() as session:
                session.add_all(
                    [
                        Reward(
                            deal_id=f"pagination-{uuid.uuid4()}",
                            agent_id=partner_id,
                            application_id=application_id,
                            reward_type=RewardType.MAIN,
                            status=RewardStatus.PENDING,
                        )
                        for _ in range(13)
                    ]
                )
                await session.commit()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.get("/admin/rewards")
                second = await client.get("/admin/rewards?page=2")
                last = await client.get("/admin/rewards?page=999999")
                invalid = await client.get("/admin/rewards?page=0")
                decision = await client.post(
                    f"/admin/rewards/{reward_id}/decide",
                    data={"decision": "approve", "page": "2", "csrf": _csrf_from(second.text)},
                )
                assert decision.status_code == 303
                assert decision.headers["location"] == "/admin/rewards?page=2"

            def ids(response):
                return set(re.findall(r'id="reward-title-(\d+)"', response.text))

            assert len(ids(first)) == 12
            assert ids(second)
            assert ids(first).isdisjoint(ids(second))
            assert 1 <= len(ids(last)) <= 12
            assert first.text.count('aria-label="Страницы начислений"') == 2
            assert 'name="page" value="2"' in second.text
            assert invalid.status_code == 422
        finally:
            app.dependency_overrides.pop(require_admin, None)
            async with session_factory() as session:
                await session.execute(delete(Reward).where(Reward.agent_id == partner_id))
                await session.commit()
            await _cleanup(admin.id, partner_id, application_id, reward_id)

    _run(scenario())
