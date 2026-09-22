from decimal import Decimal

import httpx
from pravburo_ref_common.contracts import RewardNotify
from pravburo_ref_common.models import RewardType

from src.config import get_settings


class SiteClient:
    async def notify_reward(
        self, *, agent_id: int, reward_type: RewardType, amount: Decimal | None
    ) -> None:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.site_service_url.rstrip('/')}/internal/rewards/notify",
                headers={"X-Internal-Token": settings.internal_service_token},
                json=RewardNotify(
                    agent_id=agent_id, reward_type=reward_type, amount=amount
                ).model_dump(mode="json"),
            )
        response.raise_for_status()
