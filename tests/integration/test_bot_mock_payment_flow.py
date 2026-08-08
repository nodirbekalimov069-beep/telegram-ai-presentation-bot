from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from apps.bot.handlers import presentation as presentation_handlers
from apps.bot.localization import Localizer
from core.config import Settings
from core.database.base import Base
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.orders.service import OrderService
from modules.plans.seed import seed_default_plans
from modules.users.service import UserService


@pytest.mark.asyncio
async def test_mock_payment_handler_does_not_nest_session_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mock-payment-handler.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
        user = await UserService().register(
            session,
            telegram_user_id=555,
            username=None,
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    async with factory() as session:
        order = await OrderService().create_paid_order(
            session,
            user_id=user.id,
            plan_code="medium",
            topic="AI va ta’lim",
            settings={"slide_count": 10, "language": "uz"},
        )
        order_id = order.id
        public_id = order.public_id

    telegram_user = TelegramUser(id=555, is_bot=False, first_name="Owner")
    message = Message(
        message_id=1234,
        date=datetime.now(UTC),
        chat=Chat(id=555, type="private"),
        from_user=telegram_user,
        text="Payment",
    )
    callback = CallbackQuery(
        id="mock-payment",
        from_user=telegram_user,
        chat_instance="private-test",
        message=message,
        data=f"mockpay:{public_id}",
    )
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "uz"}
    fake_presentation = SimpleNamespace(id=77, outline={"slides": []})
    generate_outline = AsyncMock(return_value=fake_presentation)
    send_outline = AsyncMock()
    edit_text = AsyncMock()
    answer_message = AsyncMock()
    answer_callback = AsyncMock()
    monkeypatch.setattr(presentation_handlers, "_generate_outline_safely", generate_outline)
    monkeypatch.setattr(presentation_handlers, "_send_outline", send_outline)
    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    try:
        async with factory() as session:
            await presentation_handlers.mock_payment(
                callback,
                state,
                session,
                Localizer.load_default(),
                Settings(
                    app_env="test",
                    payment_provider="mock",
                    mock_payment_secret=SecretStr("test-secret"),
                    ai_provider="mock",
                ),
            )
        async with factory() as session:
            saved = await session.get(Order, order_id)
        assert saved is not None and saved.status == OrderStatus.PAID
        generate_outline.assert_awaited_once()
        send_outline.assert_awaited_once()
    finally:
        await engine.dispose()
