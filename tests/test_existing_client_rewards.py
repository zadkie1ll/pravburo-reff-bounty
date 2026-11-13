import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pravburo_ref_common.database import get_session
from pravburo_ref_common.models import (
    ExistingClientImport,
    ReferralApplication,
    Reward,
    RewardType,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import service
from src.config import get_settings
from src.main import app
from src.service import HistoricalRewardSkipped, create_reward_once
from src.site_client import SiteClient


@pytest.fixture
def context(monkeypatch):
    application = ReferralApplication(id=1, agent_id=2, bitrix_deal_id="42")
    imported = ExistingClientImport(
        application_id=1,
        deal_id="42",
        excluded_reward_types=[],
        current_reward_id=None,
        imported_by_agent_id=99,
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=application)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [imported]))
    session.scalar = AsyncMock()
    for name in ("flush", "commit", "refresh", "rollback"):
        setattr(session, name, AsyncMock())
    stage_amount = AsyncMock(return_value=Decimal("3000.00"))
    overrides = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_resolve_stage_amount", stage_amount)
    monkeypatch.setattr(service, "_build_override_rewards", overrides)
    return SimpleNamespace(
        application=application, imported=imported, session=session,
        stage_amount=stage_amount, overrides=overrides,
    )


def assert_no_creation(context):
    context.stage_amount.assert_not_awaited()
    context.overrides.assert_not_awaited()
    context.session.add.assert_not_called()
    context.session.flush.assert_not_awaited()
    context.session.commit.assert_not_awaited()
    context.session.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    "reward_type", [RewardType.ADVANCE, RewardType.MAIN, RewardType.BONUS_FULL_PAYMENT]
)
def test_snapshot_excluded_milestones_skip_before_rates_or_writes(context, reward_type):
    context.imported.excluded_reward_types = [reward_type.value]
    with pytest.raises(HistoricalRewardSkipped):
        asyncio.run(create_reward_once(context.session, "42", 1, 2, reward_type))
    assert_no_creation(context)
    assert not issubclass(HistoricalRewardSkipped, ValueError)


@pytest.mark.parametrize("amount", [Decimal("12345.67"), Decimal("0.00")])
def test_current_manual_main_is_returned_unchanged_on_repeated_calls(context, amount):
    current = Reward(
        id=100, application_id=1, deal_id="42", agent_id=2,
        reward_type=RewardType.MAIN, amount=amount, source_reward_id=None,
    )
    context.imported.current_reward_id = current.id
    context.imported.excluded_reward_types = [RewardType.MAIN.value]
    context.session.get.side_effect = lambda model, key: (
        context.application if model is ReferralApplication else current
    )
    before = dict(vars(current))
    for _ in range(2):
        reward, created = asyncio.run(
            create_reward_once(context.session, "42", 1, 2, RewardType.MAIN, Decimal("99999"))
        )
        assert reward is current
        assert created is False
    assert vars(current) == before
    assert_no_creation(context)


@pytest.mark.parametrize(
    "change",
    ["request_deal", "import_application", "import_deal", "application_deal", "agent"],
)
def test_import_requires_exact_application_deal_and_agent_binding(context, change):
    deal_id, agent_id = "42", 2
    if change == "request_deal":
        deal_id = "43"
    elif change == "import_application":
        context.imported.application_id = 3
    elif change == "import_deal":
        context.imported.deal_id = "43"
    elif change == "application_deal":
        context.application.bitrix_deal_id = "43"
    else:
        agent_id = 3
    context.imported.excluded_reward_types = [RewardType.MAIN.value]
    with pytest.raises(ValueError):
        asyncio.run(create_reward_once(context.session, deal_id, 1, agent_id))
    assert_no_creation(context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("application_id", 3), ("deal_id", "43"), ("agent_id", 3),
        ("reward_type", RewardType.ADVANCE), ("source_reward_id", 99),
        ("missing", None),
    ],
)
def test_invalid_current_reward_cannot_fall_through_to_automatic_creation(context, field, value):
    current = Reward(
        id=100, application_id=1, deal_id="42", agent_id=2,
        reward_type=RewardType.MAIN, amount=Decimal("12000"), source_reward_id=None,
    )
    if field == "missing":
        current = None
    else:
        setattr(current, field, value)
    context.imported.current_reward_id = 100
    context.session.get.side_effect = lambda model, key: (
        context.application if model is ReferralApplication else current
    )
    with pytest.raises(ValueError):
        asyncio.run(create_reward_once(context.session, "42", 1, 2))
    assert_no_creation(context)


@pytest.mark.parametrize("imported", [True, False])
@pytest.mark.parametrize(
    "reward_type", [RewardType.ADVANCE, RewardType.MAIN, RewardType.BONUS_FULL_PAYMENT]
)
def test_eligible_milestones_and_normal_referrals_keep_rates_and_overrides(
    context, imported, reward_type
):
    if not imported:
        context.session.scalars.return_value = SimpleNamespace(all=lambda: [])
        # Normal referrals retain their existing attribution rules.
        context.application.bitrix_deal_id = None
    override = Reward(agent_id=3, reward_type=RewardType.OVERRIDE)
    context.overrides.return_value = [override]
    reward, created = asyncio.run(
        create_reward_once(context.session, "42", 1, 2, reward_type, Decimal("500"))
    )
    assert created is True
    assert reward.amount == (
        Decimal("3000.00") if reward_type in service.STAGE_RATE_TYPES else Decimal("500")
    )
    context.overrides.assert_awaited_once_with(context.session, reward)
    assert context.session.add.call_args_list[0].args == (reward,)
    assert context.session.add.call_args_list[1].args == (override,)
    context.session.commit.assert_awaited_once()
    context.session.refresh.assert_awaited_once_with(reward)
    # Both unique import keys must be checked, including an imported deal
    # presented with a different application that belongs to the same agent.
    statement = str(context.session.scalars.call_args.args[0])
    assert "existing_client_imports.application_id =" in statement
    assert " OR " in statement
    assert "existing_client_imports.deal_id =" in statement


def test_normal_referral_unique_conflict_still_returns_existing_without_overrides(context):
    context.session.scalars.return_value = SimpleNamespace(all=lambda: [])
    existing = Reward(id=100, amount=Decimal("7000"))
    context.session.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    context.session.scalar.return_value = existing
    reward, created = asyncio.run(create_reward_once(context.session, "42", 1, 2))
    assert reward is existing
    assert created is False
    assert reward.amount == Decimal("7000")
    context.session.rollback.assert_awaited_once()
    context.overrides.assert_not_awaited()
    context.session.commit.assert_not_awaited()


@pytest.mark.parametrize("manual_balance", [False, True])
def test_real_service_endpoint_skips_history_or_returns_manual_duplicate_without_notification(
    context, monkeypatch, manual_balance
):
    monkeypatch.setattr(get_settings(), "internal_service_token", "test-token")
    context.imported.excluded_reward_types = [RewardType.MAIN.value]
    if manual_balance:
        current = Reward(
            id=100, application_id=1, deal_id="42", agent_id=2,
            reward_type=RewardType.MAIN, amount=Decimal("12345.67"), source_reward_id=None,
        )
        context.imported.current_reward_id = current.id
        context.session.get.side_effect = lambda model, key: (
            context.application if model is ReferralApplication else current
        )
    notify = AsyncMock()
    monkeypatch.setattr(SiteClient, "notify_reward", notify)

    async def fake_session():
        yield context.session

    app.dependency_overrides[get_session] = fake_session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/internal/rewards",
                headers={"X-Internal-Token": "test-token"},
                json={"deal_id": "42", "application_id": 1, "agent_id": 2, "amount": "99999"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == (
        {"status": "duplicate", "reward_id": 100}
        if manual_balance else {"status": "historical_skipped"}
    )
    if manual_balance:
        assert current.amount == Decimal("12345.67")
    notify.assert_not_awaited()
    assert_no_creation(context)
