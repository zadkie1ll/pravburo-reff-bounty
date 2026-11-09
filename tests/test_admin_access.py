import asyncio

import pytest
from fastapi import HTTPException, Request
from pravburo_ref_common.models import Agent, AgentRole

from src.dependencies import require_admin


@pytest.mark.parametrize("role_in_cookie", ["agent", "admin"])
def test_old_session_does_not_open_bounty_admin(role_in_cookie: str) -> None:
    agent = Agent(id=1, role=AgentRole.ADMIN, is_active=True, totp_enabled=True)
    request = Request({"type": "http", "session": {"agent_id": 1, "role": role_in_cookie}})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_admin(request, agent))

    assert exc.value.status_code == 303
    assert exc.value.headers == {"Location": "/login"}


def test_session_verified_by_site_opens_bounty_admin() -> None:
    agent = Agent(id=1, role=AgentRole.ADMIN, is_active=True, totp_enabled=True)
    request = Request(
        {
            "type": "http",
            "session": {"agent_id": 1, "role": "admin", "admin_2fa_verified": True},
        }
    )

    assert asyncio.run(require_admin(request, agent)) is agent
