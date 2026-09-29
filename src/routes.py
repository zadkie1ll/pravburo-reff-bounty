import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pravburo_ref_common.contracts import RewardCreate
from pravburo_ref_common.database import get_session
from pravburo_ref_common.models import (
    Agent,
    PartnerLevel,
    PartnerLevelMonth,
    ReferralApplication,
    Reward,
    RewardStageRate,
    RewardStatus,
    RewardType,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import CurrentAdmin
from src.internal_auth import require_internal_token
from src.security import csrf_token, valid_csrf
from src.service import ADVANCE_LOOKUP_LEVEL, create_reward_once, get_stage_rate_amount
from src.site_client import SiteClient

logger = logging.getLogger(__name__)
router = APIRouter(tags=["bounty"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
Session = Annotated[AsyncSession, Depends(get_session)]

NOTIFIABLE_REWARD_TYPES = {RewardType.ADVANCE, RewardType.MAIN, RewardType.BONUS_FULL_PAYMENT}

# Причины отказа по начислению: админ выбирает из списка, а не пишет текст сам.
# Выбранный текст сохраняется в Reward.rejection_reason и показывается партнёру.
REJECTION_REASONS = (
    "Клиент отказался от услуг",
    "Не удалось связаться с клиентом",
    "Клиент уже есть в базе компании",
    "Ситуация клиента не подходит под условия программы",
    "Договор не заключён",
    "Другая причина, уточните в поддержке",
)


@router.post("/internal/rewards", dependencies=[Depends(require_internal_token)])
async def create_reward(payload: RewardCreate, session: Session) -> dict[str, object]:
    try:
        reward, created = await create_reward_once(
            session,
            payload.deal_id,
            payload.application_id,
            payload.agent_id,
            payload.reward_type,
            payload.amount,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if created and reward.reward_type in NOTIFIABLE_REWARD_TYPES:
        try:
            await SiteClient().notify_reward(
                agent_id=reward.agent_id, reward_type=reward.reward_type, amount=reward.amount
            )
        except Exception:
            logger.warning("Failed to notify site about new reward: reward_id=%s", reward.id)

    return {"status": "created" if created else "duplicate", "reward_id": reward.id}


@router.get("/admin/rewards", response_class=HTMLResponse)
async def rewards_page(request: Request, admin: CurrentAdmin, session: Session) -> HTMLResponse:
    rows = (
        await session.execute(
            select(Reward, ReferralApplication)
            .join(ReferralApplication, ReferralApplication.id == Reward.application_id)
            .order_by(Reward.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="admin_rewards.html",
        context={
            "admin": admin,
            "rows": [
                {
                    "reward": reward,
                    "application": application,
                    "phone": application.phone_normalized,
                }
                for reward, application in rows
            ],
            "rejection_reasons": REJECTION_REASONS,
            "csrf_token": csrf_token(request.session),
        },
    )


@router.post("/admin/rewards/{reward_id}/decide")
async def decide_reward(
    request: Request,
    reward_id: int,
    admin: CurrentAdmin,
    session: Session,
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form(max_length=2000)] = "",
    csrf: Annotated[str, Form()] = "",
):
    if not valid_csrf(request.session, csrf):
        return RedirectResponse("/admin/rewards", status_code=303)
    reward = await session.get(Reward, reward_id, with_for_update=True)
    if reward is None or reward.status != RewardStatus.PENDING:
        return RedirectResponse("/admin/rewards", status_code=303)
    if decision == "approve":
        reward.status = RewardStatus.APPROVED
    elif decision == "reject" and reason.strip() in REJECTION_REASONS:
        reward.status = RewardStatus.REJECTED
        reward.rejection_reason = reason.strip()
    else:
        return RedirectResponse("/admin/rewards", status_code=303)
    reward.decided_at = datetime.now(UTC)
    reward.decided_by_agent_id = admin.id
    await session.commit()
    return RedirectResponse("/admin/rewards", status_code=303)


LEVEL_LABELS = {
    PartnerLevel.START: "Старт",
    PartnerLevel.ACTIVE: "Актив",
    PartnerLevel.PRO: "Про",
    PartnerLevel.EXPERT: "Эксперт",
}


@router.get("/admin/reward-rates", response_class=HTMLResponse)
async def reward_rates_page(
    request: Request, admin: CurrentAdmin, session: Session, error: str = ""
) -> HTMLResponse:
    main_amounts = [
        {
            "level": level,
            "label": LEVEL_LABELS[level],
            "field": f"amount_main_{level}",
            "amount": await get_stage_rate_amount(session, RewardType.MAIN, level),
        }
        for level in PartnerLevel
    ]
    return templates.TemplateResponse(
        request=request,
        name="admin_reward_rates.html",
        context={
            "admin": admin,
            "advance_amount": await get_stage_rate_amount(
                session, RewardType.ADVANCE, ADVANCE_LOOKUP_LEVEL
            ),
            "main_amounts": main_amounts,
            "csrf_token": csrf_token(request.session),
            "error": error,
        },
    )


@router.post("/admin/reward-rates")
async def reward_rates_submit(
    request: Request,
    admin: CurrentAdmin,
    session: Session,
    amount_advance: Annotated[str, Form()],
    amount_main_start: Annotated[str, Form()],
    amount_main_active: Annotated[str, Form()],
    amount_main_pro: Annotated[str, Form()],
    amount_main_expert: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
):
    if not valid_csrf(request.session, csrf):
        return await reward_rates_page(request, admin, session, error="Обновите страницу")
    main_by_level = {
        PartnerLevel.START: amount_main_start,
        PartnerLevel.ACTIVE: amount_main_active,
        PartnerLevel.PRO: amount_main_pro,
        PartnerLevel.EXPERT: amount_main_expert,
    }
    try:
        advance_amount = Decimal(amount_advance)
        main_amounts = {level: Decimal(raw) for level, raw in main_by_level.items()}
    except InvalidOperation:
        return await reward_rates_page(request, admin, session, error="Укажите корректную сумму")
    if advance_amount < 0 or any(value < 0 for value in main_amounts.values()):
        return await reward_rates_page(
            request, admin, session, error="Сумма не может быть отрицательной"
        )
    for level in PartnerLevel:
        advance_rate = await session.get(RewardStageRate, (RewardType.ADVANCE, level))
        if advance_rate is not None:
            advance_rate.amount = advance_amount
        main_rate = await session.get(RewardStageRate, (RewardType.MAIN, level))
        if main_rate is not None:
            main_rate.amount = main_amounts[level]
    await session.commit()
    return RedirectResponse("/admin/reward-rates", status_code=303)


@router.get("/admin/partner-levels", response_class=HTMLResponse)
async def partner_levels_page(
    request: Request,
    admin: CurrentAdmin,
    session: Session,
    q: str = "",
    agent_id: int | None = None,
    error: str = "",
) -> HTMLResponse:
    search_results: list[Agent] = []
    if q.strip():
        pattern = f"%{q.strip()}%"
        search_results = list(
            (
                await session.scalars(
                    select(Agent)
                    .where(or_(Agent.display_name.ilike(pattern), Agent.email.ilike(pattern)))
                    .order_by(Agent.display_name)
                    .limit(20)
                )
            ).all()
        )

    selected_agent = await session.get(Agent, agent_id) if agent_id else None
    level_months: list[PartnerLevelMonth] = []
    if selected_agent is not None:
        level_months = list(
            (
                await session.scalars(
                    select(PartnerLevelMonth)
                    .where(PartnerLevelMonth.agent_id == selected_agent.id)
                    .order_by(PartnerLevelMonth.year.desc(), PartnerLevelMonth.month.desc())
                )
            ).all()
        )

    today = datetime.now(UTC)
    return templates.TemplateResponse(
        request=request,
        name="admin_partner_levels.html",
        context={
            "admin": admin,
            "q": q,
            "search_results": search_results,
            "selected_agent": selected_agent,
            "level_months": level_months,
            "levels": list(PartnerLevel),
            "level_labels": LEVEL_LABELS,
            "current_year": today.year,
            "current_month": today.month,
            "csrf_token": csrf_token(request.session),
            "error": error,
        },
    )


@router.post("/admin/partner-levels/set")
async def partner_levels_set(
    request: Request,
    admin: CurrentAdmin,
    session: Session,
    agent_id: Annotated[int, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    level: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
):
    redirect_url = f"/admin/partner-levels?agent_id={agent_id}"
    if not valid_csrf(request.session, csrf):
        return RedirectResponse(redirect_url, status_code=303)
    if level not in {member.value for member in PartnerLevel} or not (1 <= month <= 12):
        return RedirectResponse(f"{redirect_url}&error=Некорректные данные", status_code=303)

    existing = await session.scalar(
        select(PartnerLevelMonth).where(
            PartnerLevelMonth.agent_id == agent_id,
            PartnerLevelMonth.year == year,
            PartnerLevelMonth.month == month,
        )
    )
    if existing is None:
        session.add(
            PartnerLevelMonth(
                agent_id=agent_id,
                year=year,
                month=month,
                contracts_count=0,
                level=PartnerLevel(level),
                is_manual=True,
            )
        )
    else:
        existing.level = PartnerLevel(level)
        existing.is_manual = True
    await session.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/admin/partner-levels/reset-to-auto")
async def partner_levels_reset_to_auto(
    request: Request,
    admin: CurrentAdmin,
    session: Session,
    agent_id: Annotated[int, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    csrf: Annotated[str, Form()] = "",
):
    redirect_url = f"/admin/partner-levels?agent_id={agent_id}"
    if not valid_csrf(request.session, csrf):
        return RedirectResponse(redirect_url, status_code=303)
    existing = await session.scalar(
        select(PartnerLevelMonth).where(
            PartnerLevelMonth.agent_id == agent_id,
            PartnerLevelMonth.year == year,
            PartnerLevelMonth.month == month,
        )
    )
    if existing is not None:
        existing.is_manual = False
        await session.commit()
    return RedirectResponse(redirect_url, status_code=303)
