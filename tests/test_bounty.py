from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pravburo_ref_common.database import get_session
from pravburo_ref_common.models import RewardType

from src import routes
from src.config import get_settings
from src.main import app
from src.site_client import SiteClient


def test_internal_reward_endpoint_requires_token() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/internal/rewards",
            json={"deal_id": "42", "application_id": 1, "agent_id": 2},
        )
    assert response.status_code == 401


def test_internal_reward_endpoint_returns_idempotency_result(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_service_token", "test-token")

    async def fake_session():
        yield SimpleNamespace()

    async def fake_create_reward_once(
        session, deal_id, application_id, agent_id, reward_type, amount
    ):
        del session
        assert (deal_id, application_id, agent_id) == ("42", 1, 2)
        assert reward_type == RewardType.MAIN
        assert amount is None
        return SimpleNamespace(id=100), False

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(routes, "create_reward_once", fake_create_reward_once)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/internal/rewards",
                headers={"X-Internal-Token": "test-token"},
                json={"deal_id": "42", "application_id": 1, "agent_id": 2},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "reward_id": 100}


def test_internal_reward_endpoint_notifies_site_for_new_advance(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_service_token", "test-token")

    async def fake_session():
        yield SimpleNamespace()

    async def fake_create_reward_once(
        session, deal_id, application_id, agent_id, reward_type, amount
    ):
        del session, deal_id, application_id, agent_id, amount
        return (
            SimpleNamespace(
                id=101, agent_id=2, reward_type=reward_type, amount=Decimal("3000.00")
            ),
            True,
        )

    notified: list[dict] = []

    async def fake_notify_reward(self, *, agent_id, reward_type, amount):
        del self
        notified.append({"agent_id": agent_id, "reward_type": reward_type, "amount": amount})

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(routes, "create_reward_once", fake_create_reward_once)
    monkeypatch.setattr(SiteClient, "notify_reward", fake_notify_reward)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/internal/rewards",
                headers={"X-Internal-Token": "test-token"},
                json={
                    "deal_id": "42",
                    "application_id": 1,
                    "agent_id": 2,
                    "reward_type": "advance",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "created", "reward_id": 101}
    assert notified == [
        {"agent_id": 2, "reward_type": RewardType.ADVANCE, "amount": Decimal("3000.00")}
    ]


def test_internal_reward_endpoint_does_not_notify_for_override(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_service_token", "test-token")

    async def fake_session():
        yield SimpleNamespace()

    async def fake_create_reward_once(
        session, deal_id, application_id, agent_id, reward_type, amount
    ):
        del session, deal_id, application_id, agent_id, amount
        return (
            SimpleNamespace(id=102, agent_id=2, reward_type=reward_type, amount=None),
            True,
        )

    notified: list[dict] = []

    async def fake_notify_reward(self, **kwargs):
        del self
        notified.append(kwargs)

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(routes, "create_reward_once", fake_create_reward_once)
    monkeypatch.setattr(SiteClient, "notify_reward", fake_notify_reward)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/internal/rewards",
                headers={"X-Internal-Token": "test-token"},
                json={
                    "deal_id": "42",
                    "application_id": 1,
                    "agent_id": 2,
                    "reward_type": "override",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert notified == []
