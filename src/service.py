from decimal import Decimal
from zoneinfo import ZoneInfo

from pravburo_ref_common.models import (
    Agent,
    AgentCredential,
    AgentIdentity,
    ExistingClientImport,
    NetworkOverrideRate,
    PartnerLevel,
    PartnerLevelMonth,
    ReferralApplication,
    Reward,
    RewardStageRate,
    RewardType,
)
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

STAGE_RATE_TYPES = {RewardType.ADVANCE, RewardType.MAIN}

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Advance is a flat 3000 regardless of partner level (see reward_stage_rates'
# docstring) - all 4 level rows hold the same amount, so any one of them works
# as the lookup key.
ADVANCE_LOOKUP_LEVEL = PartnerLevel.START


class HistoricalRewardSkipped(Exception):
    """An imported client's snapshot excludes this automatic reward stage."""


async def _check_existing_client_import(
    session: AsyncSession,
    application: ReferralApplication,
    deal_id: str,
    agent_id: int,
    reward_type: RewardType,
) -> Reward | None:
    # Check both keys: querying only the application would allow the imported
    # deal to be paid again through a different, otherwise valid application.
    imports = (
        await session.scalars(
            select(ExistingClientImport).where(
                or_(
                    ExistingClientImport.application_id == application.id,
                    ExistingClientImport.deal_id == deal_id,
                )
            )
        )
    ).all()
    for imported in imports:
        if (
            imported.application_id != application.id
            or imported.deal_id != deal_id
            or application.bitrix_deal_id != deal_id
            or application.agent_id != agent_id
        ):
            raise ValueError("Imported client attribution does not match")

        if reward_type == RewardType.MAIN and imported.current_reward_id is not None:
            # The site creates this manual balance as MAIN. A retry must neither
            # reprice it using stage rates nor create upline rewards for it.
            current = await session.get(Reward, imported.current_reward_id)
            if current is None or (
                current.application_id != application.id
                or current.deal_id != deal_id
                or current.agent_id != agent_id
                or current.reward_type != RewardType.MAIN
                or current.source_reward_id is not None
            ):
                raise ValueError("Imported client current reward does not match")
            return current

        if reward_type.value in imported.excluded_reward_types:
            raise HistoricalRewardSkipped()
    return None


async def get_stage_rate_amount(
    session: AsyncSession, reward_type: RewardType, level: PartnerLevel
) -> Decimal | None:
    rate = await session.get(RewardStageRate, (reward_type, level))
    return rate.amount if rate is not None else None


async def _origin_month(
    session: AsyncSession, deal_id: str, agent_id: int
) -> tuple[int, int] | None:
    """The (year, month) the contract for this deal was signed in, taken from
    the sibling ADVANCE reward's created_at - MAIN's amount depends on the
    partner's level for that month, not the month MAIN itself is created in
    (the deposit can be paid much later). If an imported client already earned
    ADVANCE before transfer, accounting starts in the import's Moscow month.
    """
    signed_at = await session.scalar(
        select(Reward.created_at).where(
            Reward.deal_id == deal_id,
            Reward.agent_id == agent_id,
            Reward.reward_type == RewardType.ADVANCE,
        )
    )
    if signed_at is None:
        imported = await session.scalar(
            select(ExistingClientImport)
            .join(
                ReferralApplication,
                ReferralApplication.id == ExistingClientImport.application_id,
            )
            .where(
                ExistingClientImport.deal_id == deal_id,
                ReferralApplication.bitrix_deal_id == deal_id,
                ReferralApplication.agent_id == agent_id,
                ExistingClientImport.current_reward_id.is_(None),
            )
        )
        if imported is None or RewardType.ADVANCE.value not in imported.excluded_reward_types:
            return None
        signed_at = imported.created_at
    moscow = signed_at.astimezone(MOSCOW_TZ)
    return moscow.year, moscow.month


async def _resolve_stage_amount(
    session: AsyncSession, reward_type: RewardType, deal_id: str, agent_id: int
) -> Decimal | None:
    if reward_type == RewardType.ADVANCE:
        return await get_stage_rate_amount(session, RewardType.ADVANCE, ADVANCE_LOOKUP_LEVEL)

    origin = await _origin_month(session, deal_id, agent_id)
    if origin is None:
        return None
    year, month = origin
    level_month = await session.scalar(
        select(PartnerLevelMonth).where(
            PartnerLevelMonth.agent_id == agent_id,
            PartnerLevelMonth.year == year,
            PartnerLevelMonth.month == month,
        )
    )
    if level_month is None:
        # Origin month isn't closed yet (see pravburo-reff-site's
        # run_monthly_level_close) - the amount is filled in once it is.
        return None
    return await get_stage_rate_amount(session, RewardType.MAIN, level_month.level)


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
    current = await _check_existing_client_import(
        session, application, deal_id, agent_id, reward_type
    )
    if current is not None:
        return current, False
    if reward_type in STAGE_RATE_TYPES:
        amount = await _resolve_stage_amount(session, reward_type, deal_id, agent_id)
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
