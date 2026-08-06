from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers import presentation as presentation_handlers
from apps.bot.localization import Localizer
from core.config import Settings


@pytest.mark.asyncio
async def test_approve_outline_reuses_preview_as_durable_progress_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram_user = User(id=555, is_bot=False, first_name="Owner")
    message = Message(
        message_id=1234,
        date=datetime.now(UTC),
        chat=Chat(id=555, type="private"),
        from_user=telegram_user,
        text="Outline",
    )
    callback = CallbackQuery(
        id="callback-1",
        from_user=telegram_user,
        chat_instance="private-test",
        message=message,
        data="outline:approve:7",
    )
    state = AsyncMock(spec=FSMContext)
    session = AsyncMock(spec=AsyncSession)
    events: list[str] = []
    job = Mock(id=91, status="QUEUED")
    service = Mock()

    async def approve(*args: object, **kwargs: object) -> object:
        del args, kwargs
        events.append("database")
        return job

    async def edit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("telegram")

    service.approve_outline = AsyncMock(side_effect=approve)
    edit_text = AsyncMock(side_effect=edit)
    answer_callback = AsyncMock()

    monkeypatch.setattr(
        presentation_handlers,
        "_authorized_presentation_context",
        AsyncMock(return_value=(7, 8, "uz")),
    )
    monkeypatch.setattr(presentation_handlers, "build_ai_provider", Mock(return_value=Mock()))
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))
    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await presentation_handlers.approve_outline(
        callback,
        state,
        session,
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    edit_text.assert_awaited_once_with(
        "0% · ⏳ Buyurtma navbatga qo‘yildi. Holat shu xabarda yangilanadi."
    )
    service.approve_outline.assert_awaited_once_with(
        session,
        presentation_id=7,
        progress_chat_id=555,
        progress_message_id=1234,
    )
    state.set_state.assert_awaited_once()
    answer_callback.assert_awaited_once_with()
    assert events == ["database", "telegram"]


@pytest.mark.asyncio
async def test_outline_timeout_returns_friendly_error_and_logs_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    responder = SimpleNamespace(answer=AsyncMock())
    service = Mock()
    service.generate_outline = AsyncMock(side_effect=TimeoutError("provider timed out"))
    provider = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(presentation_handlers, "build_ai_provider", Mock(return_value=provider))
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))

    with caplog.at_level(logging.ERROR, logger="apps.bot.handlers.presentation"):
        result = await presentation_handlers._generate_outline_safely(
            responder=responder,
            session=AsyncMock(spec=AsyncSession),
            settings=Settings(app_env="test"),
            localizer=Localizer.load_default(),
            language="uz",
            order_id=8,
            telegram_user_id=555,
        )

    assert result is None
    responder.answer.assert_awaited_once_with(
        "⚠️ AI javob berishda vaqtinchalik muammo yuz berdi. Qayta urinib ko‘ring."
    )
    provider.aclose.assert_awaited_once_with()
    assert "order_id=8" in caplog.text
    assert "telegram_user_id=555" in caplog.text
    assert "correlation_id=" in caplog.text
    assert "TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_outline_preview_can_replace_ai_progress_message() -> None:
    progress_message = SimpleNamespace(edit_text=AsyncMock())
    presentation = SimpleNamespace(
        id=7,
        outline={"slides": [{"number": 1, "title": "Kirish"}]},
    )

    await presentation_handlers._send_outline(
        progress_message,
        presentation,
        Localizer.load_default(),
        "uz",
        edit=True,
    )

    progress_message.edit_text.assert_awaited_once()
    assert "1. Kirish" in progress_message.edit_text.await_args.args[0]
    assert progress_message.edit_text.await_args.kwargs["reply_markup"] is not None
