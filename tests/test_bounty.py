from types import SimpleNamespace

from fastapi.testclient import TestClient
from pravburo_ref_common.database import get_session

from src import routes
from src.config import get_settings
from src.main import app


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

    async def fake_create_reward_once(session, deal_id, application_id, agent_id):
        del session
        assert (deal_id, application_id, agent_id) == ("42", 1, 2)
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
