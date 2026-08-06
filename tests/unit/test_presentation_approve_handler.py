from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers import presentation as presentation_handlers
from apps.bot.localization import Localizer
from apps.bot.fsm import PresentationFlow
from core.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "maximum"),
    [
        (presentation_handlers.purpose, 300),
        (presentation_handlers.audience, 200),
    ],
)
async def test_short_free_text_fields_are_rejected_before_order_creation(
    handler: Any,
    maximum: int,
) -> None:
    message = SimpleNamespace(text="x", answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "uz"}

    await handler(message, state, Localizer.load_default())

    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once_with(f"Javob 2–{maximum} belgi oralig‘ida bo‘lishi kerak.")


@pytest.mark.asyncio
async def test_text_cancel_does_not_report_success_while_outline_is_generating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"presentation_id": 7}
    state.get_state.return_value = PresentationFlow.outline_edit.state
    session = AsyncMock(spec=AsyncSession)
    service = Mock(
        cancel_outline=AsyncMock(side_effect=ValueError("outline is no longer cancellable"))
    )
    menu = AsyncMock()
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))
    monkeypatch.setattr(presentation_handlers, "_language", AsyncMock(return_value="uz"))
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.cancel_flow(
        message,
        state,
        session,
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    service.cancel_outline.assert_awaited_once_with(session, presentation_id=7)
    state.clear.assert_not_awaited()
    menu.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "Reja hozir yangilanmoqda. Jarayon tugagach qayta urinib ko‘ring."
    )


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
    service.fail_outline_before_start = AsyncMock(return_value=False)
    provider = SimpleNamespace(aclose=AsyncMock())
    session = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(presentation_handlers, "build_ai_provider", Mock(return_value=provider))
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))

    with caplog.at_level(logging.ERROR, logger="apps.bot.handlers.presentation"):
        result = await presentation_handlers._generate_outline_safely(
            responder=responder,
            session=session,
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
    service.fail_outline_before_start.assert_awaited_once_with(
        session,
        order_id=8,
        reason="AI provider initialization or outline generation failed",
    )
    assert "order_id=8" in caplog.text
    assert "telegram_user_id=555" in caplog.text
    assert "correlation_id=" in caplog.text
    assert "TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_provider_factory_failure_runs_pre_start_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder = SimpleNamespace(answer=AsyncMock())
    session = AsyncMock(spec=AsyncSession)
    service = Mock(fail_outline_before_start=AsyncMock(return_value=True))
    monkeypatch.setattr(
        presentation_handlers,
        "build_ai_provider",
        Mock(side_effect=RuntimeError("provider configuration failed")),
    )
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))

    result = await presentation_handlers._generate_outline_safely(
        responder=responder,
        session=session,
        settings=Settings(app_env="test"),
        localizer=Localizer.load_default(),
        language="uz",
        order_id=9,
        telegram_user_id=555,
    )

    assert result is None
    service.fail_outline_before_start.assert_awaited_once_with(
        session,
        order_id=9,
        reason="AI provider initialization or outline generation failed",
    )
    responder.answer.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_outline_cancel_persists_cancellation_before_clearing_ui(
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
        id="callback-cancel",
        from_user=telegram_user,
        chat_instance="private-test",
        message=message,
        data="outline:cancel:7",
    )
    state = AsyncMock(spec=FSMContext)
    session = AsyncMock(spec=AsyncSession)
    service = Mock(
        cancel_outline=AsyncMock(
            side_effect=[True, False, ValueError("outline is no longer cancellable")]
        )
    )
    edit_text = AsyncMock()
    answer_callback = AsyncMock()
    menu = AsyncMock()
    user = SimpleNamespace(id=9)

    monkeypatch.setattr(
        presentation_handlers,
        "_authorized_presentation_context",
        AsyncMock(return_value=(7, 8, "uz")),
    )
    monkeypatch.setattr(presentation_handlers, "PresentationService", Mock(return_value=service))
    monkeypatch.setattr(presentation_handlers, "current_user", AsyncMock(return_value=user))
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)
    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    settings = Settings(app_env="test")
    localizer = Localizer.load_default()
    await presentation_handlers.cancel_outline(
        callback,
        state,
        session,
        localizer,
        settings,
    )
    await presentation_handlers.cancel_outline(
        callback,
        state,
        session,
        localizer,
        settings,
    )
    await presentation_handlers.cancel_outline(
        callback,
        state,
        session,
        localizer,
        settings,
    )

    assert service.cancel_outline.await_count == 3
    assert state.clear.await_count == 2
    edit_text.assert_awaited_once_with(localizer.text("uz", "create.cancelled"))
    menu.assert_awaited_once_with(
        message,
        user=user,
        localizer=localizer,
        settings=settings,
    )
    assert answer_callback.await_count == 3
    answer_callback.assert_any_await(
        localizer.text("uz", "error.outline_busy"),
        show_alert=True,
    )
