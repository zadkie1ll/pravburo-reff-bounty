from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pravburo_ref_common.contracts import RewardCreate
from pravburo_ref_common.database import get_session
from pravburo_ref_common.models import ReferralApplication, Reward, RewardStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import CurrentAdmin
from src.internal_auth import require_internal_token
from src.security import csrf_token, valid_csrf
from src.service import create_reward_once

router = APIRouter(tags=["bounty"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
Session = Annotated[AsyncSession, Depends(get_session)]


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
    elif decision == "reject" and reason.strip():
        reward.status = RewardStatus.REJECTED
        reward.rejection_reason = reason.strip()
    else:
        return RedirectResponse("/admin/rewards", status_code=303)
    reward.decided_at = datetime.now(UTC)
    reward.decided_by_agent_id = admin.id
    await session.commit()
    return RedirectResponse("/admin/rewards", status_code=303)
