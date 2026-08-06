from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.main import create_app
from core.config import Settings
from core.database.base import Base
import core.database.models  # noqa: F401
from modules.plans.seed import seed_default_plans


@pytest.fixture
async def api_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        secret_key=SecretStr("test-secret-with-sufficient-length"),
        mock_payment_secret=SecretStr("mock-secret"),
        telegram_admin_ids="999",
        admin_api_token=SecretStr("admin-test-token"),
        telegram_webhook_secret=SecretStr("telegram-webhook-secret"),
    )
    app = create_app(settings=settings, session_factory=factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await engine.dispose()


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints(api_client: AsyncClient) -> None:
    health = await api_client.get("/health/live")
    readiness = await api_client.get("/health/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_active_plans_are_exposed_from_database(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/v1/plans")

    assert response.status_code == 200
    payload = response.json()
    assert [item["code"] for item in payload] == [
        "free_demo",
        "medium",
        "premium",
        "premium_plus",
    ]
    assert payload[1]["price_uzs"] == 3_000


@pytest.mark.asyncio
async def test_admin_stats_requires_admin_identity(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/v1/admin/stats")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_update_database_backed_plan(api_client: AsyncClient) -> None:
    headers = {
        "X-Telegram-User-ID": "999",
        "Authorization": "Bearer admin-test-token",
    }
    response = await api_client.patch(
        "/api/v1/admin/plans/medium",
        headers=headers,
        json={"price_uzs": 3500, "maximum_slides": 14},
    )
    assert response.status_code == 200
    assert response.json()["price_uzs"] == 3500
    assert response.json()["maximum_slides"] == 14


@pytest.mark.asyncio
async def test_telegram_webhook_fails_closed_on_wrong_secret(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"update_id": 1},
    )
    assert response.status_code == 403
