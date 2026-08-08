from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.main import create_app
from apps.api.dependencies import require_admin_header
from core.config import Settings


def _production_settings(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "app_env": "production",
        "secret_key": SecretStr("wQ9-zT7-mN4-pL8-sV2-xC6-kR5-eH3-production"),
        "telegram_bot_token": SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        "bot_private_mode": True,
        "owner_telegram_id": 42,
        "ai_provider": "openai",
        "ai_api_key": SecretStr("test-openai-key"),
        "payment_provider": "click",
        "click_merchant_id": SecretStr("merchant"),
        "click_service_id": SecretStr("service"),
        "click_secret_key": SecretStr("click-secret"),
        "admin_api_token": SecretStr("adm-9Kx7-Qp4-Nv8-Tm2-Rc6-Wz5"),
    }
    values.update(overrides)
    return values


def test_production_rejects_mock_ai_provider() -> None:
    with pytest.raises(ValidationError, match="mock AI"):
        Settings(**_production_settings(ai_provider="mock", ai_api_key=SecretStr("")))


@pytest.mark.parametrize(
    ("field", "value", "error_name"),
    [
        ("secret_key", "replace-with-at-least-32-random-characters", "SECRET_KEY"),
        ("secret_key", "abcdefg" + " " * 25, "SECRET_KEY"),
        ("secret_key", "abcdefgh" * 4, "SECRET_KEY"),
        ("secret_key", "password12345678password12345678", "SECRET_KEY"),
        ("admin_api_token", "your-admin-token", "ADMIN_API_TOKEN"),
    ],
)
def test_production_rejects_weak_or_placeholder_secrets(
    field: str,
    value: str,
    error_name: str,
) -> None:
    with pytest.raises(ValidationError, match=error_name):
        Settings(**_production_settings(**{field: SecretStr(value)}))


def test_production_webhook_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(
            **_production_settings(
                telegram_mode="webhook",
                telegram_webhook_secret=SecretStr("change-me-webhook-secret"),
            )
        )


@pytest.mark.asyncio
async def test_production_mock_payment_webhook_is_not_available(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'production-api.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        settings=Settings(**_production_settings()),
        session_factory=factory,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/payments/mock/webhook", content=b"{}")
        schema = (await client.get("/openapi.json")).json()

    assert response.status_code == 404
    assert "/api/v1/payments/mock/webhook" not in schema["paths"]
    await engine.dispose()


def test_payment_signing_secrets_are_scoped_to_api_and_bot() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]
    signing_keys = {
        "CLICK_MERCHANT_ID",
        "CLICK_SERVICE_ID",
        "CLICK_SECRET_KEY",
        "PAYME_MERCHANT_ID",
        "PAYME_LOGIN",
        "PAYME_SECRET_KEY",
    }

    for service_name in ("api", "bot"):
        assert signing_keys <= services[service_name]["environment"].keys()
    for service_name in ("migrate", "worker", "outbox"):
        assert signing_keys.isdisjoint(services[service_name]["environment"].keys())


def test_compose_defaults_bot_off_and_binds_api_to_loopback() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert compose["x-app-environment"]["BOT_ENABLED"] == "${BOT_ENABLED:-false}"
    assert compose["services"]["api"]["ports"] == [
        "${API_BIND_HOST:-127.0.0.1}:${API_PORT:-8000}:8000"
    ]


def test_env_example_documents_safe_private_bot_switches() -> None:
    env_example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")

    assert "BOT_ENABLED=false" in env_example
    assert "BOT_PRIVATE_MODE=true" in env_example


def test_admin_identity_header_cannot_authenticate_without_bearer_secret() -> None:
    settings = Settings(
        app_env="test",
        telegram_admin_ids="999",
        admin_api_token=SecretStr(""),
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        require_admin_header(
            request,
            x_telegram_user_id="999",
            authorization=None,
        )

    assert exc_info.value.status_code == 403
