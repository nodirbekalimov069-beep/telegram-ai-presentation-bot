from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import core.database.models  # noqa: F401
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.api.main import create_app
from apps.bot.handlers.presentation import choose_plan
from apps.bot.localization import Localizer
from apps.bot.middleware import OwnerOnlyMiddleware
from core.config import Settings
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.payments.models import Payment
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService
from modules.users.service import UserService


class _RecordingMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def _disabled_production_settings() -> Settings:
    return Settings(
        app_env="production",
        secret_key=SecretStr("wQ9-zT7-mN4-pL8-sV2-xC6-kR5-eH3-production"),
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        telegram_admin_ids="99",
        admin_api_token=SecretStr("adm-9Kx7-Qp4-Nv8-Tm2-Rc6-Wz5"),
        bot_private_mode=True,
        owner_telegram_id=42,
        ai_provider="openai",
        ai_api_key=SecretStr("test-openai-key"),
        payment_provider="disabled",
    )


@pytest.fixture
async def disabled_owner_db(
    tmp_path: Path,
) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'disabled-owner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=42,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _durable_counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int, int]:
    async with factory() as session:
        return (
            int(await session.scalar(select(func.count(ServiceOrder.id))) or 0),
            int(await session.scalar(select(func.count(OutboxEvent.id))) or 0),
            int(await session.scalar(select(func.count(Payment.id))) or 0),
            int(
                await session.scalar(
                    select(func.count(ServiceOrder.id)).where(
                        or_(
                            ServiceOrder.docx_storage_key.is_not(None),
                            ServiceOrder.pdf_storage_key.is_not(None),
                        )
                    )
                )
                or 0
            ),
        )


@pytest.mark.asyncio
async def test_disabled_payment_non_owner_telegram_rejects_before_all_durable_mutation(
    disabled_owner_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_call = AsyncMock()
    payment_call = Mock()
    monkeypatch.setattr(
        "modules.ai.providers.openai_compatible.OpenAICompatibleProvider.generate_quiz",
        provider_call,
    )
    monkeypatch.setattr("modules.payments.factory.build_payment_provider", payment_call)
    middleware = OwnerOnlyMiddleware(private_mode=True, owner_telegram_id=42)
    event = SimpleNamespace(message=_RecordingMessage(), callback_query=None)

    async def create_order(*_args: object, **_kwargs: object) -> None:
        async with disabled_owner_db() as session:
            await ServiceOrderService().create_quiz_order(
                session,
                telegram_user_id=99,
                request_payload={
                    "topic": "Algoritmik fikrlash",
                    "question_count": 4,
                    "difficulty": "medium",
                    "option_count": 4,
                    "answer_key": True,
                    "language": "uz",
                    "requirements": "Amaliy vaziyatlarga e’tibor bering",
                },
                idempotency_key="non-owner-must-not-run",
            )

    before = await _durable_counts(disabled_owner_db)
    result = await middleware(create_order, event, {"event_from_user": SimpleNamespace(id=99)})
    after = await _durable_counts(disabled_owner_db)

    assert result is None
    assert after == before == (0, 0, 0, 0)
    provider_call.assert_not_awaited()
    payment_call.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_payment_owner_can_create_quiz_without_payment_or_provider_call(
    disabled_owner_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_call = AsyncMock()
    payment_call = Mock()
    monkeypatch.setattr(
        "modules.ai.providers.openai_compatible.OpenAICompatibleProvider.generate_quiz",
        provider_call,
    )
    monkeypatch.setattr("modules.payments.factory.build_payment_provider", payment_call)
    middleware = OwnerOnlyMiddleware(private_mode=True, owner_telegram_id=42)
    event = SimpleNamespace(message=_RecordingMessage(), callback_query=None)

    async def create_order(*_args: object, **_kwargs: object) -> int:
        async with disabled_owner_db() as session:
            order = await ServiceOrderService().create_quiz_order(
                session,
                telegram_user_id=42,
                request_payload={
                    "topic": "Algoritmik fikrlash",
                    "question_count": 4,
                    "difficulty": "medium",
                    "option_count": 4,
                    "answer_key": True,
                    "language": "uz",
                    "requirements": "Amaliy vaziyatlarga e’tibor bering",
                },
                idempotency_key="owner-disabled-payment-quiz",
            )
            return order.id

    result = await middleware(create_order, event, {"event_from_user": SimpleNamespace(id=42)})

    assert isinstance(result, int)
    assert await _durable_counts(disabled_owner_db) == (1, 1, 0, 0)
    provider_call.assert_not_awaited()
    payment_call.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_payment_paid_presentation_rejects_before_order_creation(
    disabled_owner_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    localizer = Localizer.load_default()
    create_paid_order = AsyncMock()
    payment_call = Mock()
    monkeypatch.setattr(
        "apps.bot.handlers.presentation.OrderService.create_paid_order",
        create_paid_order,
    )
    monkeypatch.setattr(
        "apps.bot.handlers.presentation.build_payment_provider",
        payment_call,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        text=localizer.option_label("uz", "medium"),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "language": "uz",
                "topic": "Algoritmik fikrlash",
            }
        ),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )

    async with disabled_owner_db() as session:
        await choose_plan(
            message,
            state,
            session,
            localizer,
            _disabled_production_settings(),
        )

    create_paid_order.assert_not_awaited()
    payment_call.assert_not_called()
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once_with(localizer.text("uz", "payment.disabled"))
    assert await _durable_counts(disabled_owner_db) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_disabled_payment_http_surfaces_are_absent_without_durable_mutation(
    disabled_owner_db: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        settings=_disabled_production_settings(),
        session_factory=disabled_owner_db,
    )
    before = await _durable_counts(disabled_owner_db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        schema = (await client.get("/openapi.json")).json()
        responses = [
            await client.post("/api/v1/payments/click", content=b""),
            await client.post("/api/v1/payments/payme", content=b"{}"),
            await client.post("/api/v1/payments/mock/webhook", content=b"{}"),
        ]

    assert "/api/v1/payments/click" not in schema["paths"]
    assert "/api/v1/payments/payme" not in schema["paths"]
    assert "/api/v1/payments/mock/webhook" not in schema["paths"]
    assert [response.status_code for response in responses] == [404, 404, 404]
    assert await _durable_counts(disabled_owner_db) == before == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_disabled_payment_api_order_surfaces_are_owner_only_before_mutation(
    disabled_owner_db: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        settings=_disabled_production_settings(),
        session_factory=disabled_owner_db,
    )
    headers = {
        "X-Telegram-User-ID": "99",
        "Authorization": "Bearer adm-9Kx7-Qp4-Nv8-Tm2-Rc6-Wz5",
    }
    before = await _durable_counts(disabled_owner_db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        list_response = await client.get("/api/v1/admin/orders", headers=headers)
        retry_response = await client.post(
            "/api/v1/admin/presentations/999/retry",
            headers=headers,
        )

    after = await _durable_counts(disabled_owner_db)
    assert list_response.status_code == 403
    assert retry_response.status_code == 403
    assert after == before == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_disabled_payment_owner_with_strong_bearer_can_access_order_surface(
    disabled_owner_db: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        settings=_disabled_production_settings(),
        session_factory=disabled_owner_db,
    )
    headers = {
        "X-Telegram-User-ID": "42",
        "Authorization": "Bearer adm-9Kx7-Qp4-Nv8-Tm2-Rc6-Wz5",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/admin/orders", headers=headers)

    assert response.status_code == 200
    assert response.json() == []
    assert await _durable_counts(disabled_owner_db) == (0, 0, 0, 0)
