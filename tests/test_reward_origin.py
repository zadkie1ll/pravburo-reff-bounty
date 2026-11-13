import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pravburo_ref_common.models import (
    ExistingClientImport,
    PartnerLevel,
    PartnerLevelMonth,
    ReferralApplication,
    RewardStageRate,
    RewardType,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src import service


@pytest.mark.parametrize("closed_month", [True, False])
def test_imported_main_uses_transfer_moscow_month_without_fabricating_advance(
    monkeypatch, closed_month
):
    application = ReferralApplication(id=1, agent_id=2, bitrix_deal_id="42")
    imported = ExistingClientImport(
        application_id=1, deal_id="42", current_reward_id=None,
        excluded_reward_types=[RewardType.ADVANCE.value], imported_by_agent_id=99,
        # UTC December becomes January of the next year in Moscow.
        created_at=datetime(2025, 12, 31, 22, 30, tzinfo=UTC),
    )
    level_month = PartnerLevelMonth(
        agent_id=2, year=2026, month=1, level=PartnerLevel.PRO
    ) if closed_month else None
    rate = RewardStageRate(
        reward_type=RewardType.MAIN, level=PartnerLevel.PRO, amount=Decimal("18000")
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(side_effect=lambda model, key: (
        application if model is ReferralApplication else rate
    ))
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [imported]))
    session.scalar = AsyncMock(side_effect=[None, imported, level_month])
    overrides = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_build_override_rewards", overrides)

    reward, created = asyncio.run(service.create_reward_once(
        session, "42", 1, 2, RewardType.MAIN, Decimal("99999")
    ))

    assert created is True
    assert reward.amount == (rate.amount if closed_month else None)
    assert reward.reward_type == RewardType.MAIN
    # Only the future MAIN is written; no synthetic historical ADVANCE.
    session.add.assert_called_once_with(reward)
    overrides.assert_awaited_once_with(session, reward)
    level_query = session.scalar.await_args_list[2].args[0]
    assert level_query.compile().params == {"agent_id_1": 2, "year_1": 2026, "month_1": 1}
    import_query = session.scalar.await_args_list[1].args[0]
    assert import_query.compile().params == {
        "deal_id_1": "42", "bitrix_deal_id_1": "42", "agent_id_1": 2
    }
    assert "current_reward_id IS NULL" in str(import_query)
    if closed_month:
        session.get.assert_any_await(RewardStageRate, (RewardType.MAIN, PartnerLevel.PRO))
    else:
        session.get.assert_awaited_once_with(ReferralApplication, 1)


def test_sibling_advance_still_takes_precedence_for_origin_month():
    session = MagicMock(spec=AsyncSession)
    # An existing advance remains authoritative for imports and normal referrals;
    # the import fallback must not be queried in either case.
    session.scalar = AsyncMock(return_value=datetime(2025, 5, 31, 22, tzinfo=UTC))
    assert asyncio.run(service._origin_month(session, "42", 2)) == (2025, 6)
    session.scalar.assert_awaited_once()
    query = session.scalar.await_args.args[0]
    assert query.compile().params == {
        "deal_id_1": "42", "agent_id_1": 2, "reward_type_1": RewardType.ADVANCE
    }


@pytest.mark.parametrize("excluded", [None, [], [RewardType.BONUS_FULL_PAYMENT.value]])
def test_missing_advance_without_historical_advance_exclusion_keeps_main_unpriced(excluded):
    imported = None if excluded is None else ExistingClientImport(
        excluded_reward_types=excluded, created_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[None, imported])
    assert asyncio.run(service._resolve_stage_amount(session, RewardType.MAIN, "42", 2)) is None
    session.get.assert_not_awaited()
    session.add.assert_not_called()
