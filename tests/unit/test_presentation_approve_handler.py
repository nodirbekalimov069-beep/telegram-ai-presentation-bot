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
async def test_waiting_payment_cancel_persists_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"order_id": 8, "language": "uz"}
    state.get_state.return_value = PresentationFlow.waiting_payment.state
    session = AsyncMock(spec=AsyncSession)
    events: list[str] = []

    async def cancel_payment(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        events.append("cancel")
        return True

    order_service = Mock(cancel_waiting_payment=AsyncMock(side_effect=cancel_payment))
    user = SimpleNamespace(id=9, selected_language="uz")
    menu = AsyncMock()

    async def load_user(*args: object, **kwargs: object) -> object:
        del args, kwargs
        events.append("user")
        return user

    monkeypatch.setattr(presentation_handlers, "OrderService", Mock(return_value=order_service))
    monkeypatch.setattr(presentation_handlers, "current_user", AsyncMock(side_effect=load_user))
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.cancel_flow(
        message,
        state,
        session,
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    order_service.cancel_waiting_payment.assert_awaited_once_with(
        session,
        order_id=8,
        telegram_user_id=555,
    )
    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once_with("Jarayon bekor qilindi.")
    menu.assert_awaited_once()
    assert events[0] == "cancel"


@pytest.mark.asyncio
async def test_stale_cancel_button_does_not_claim_an_active_job_was_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {}
    state.get_state.return_value = None
    user = SimpleNamespace(id=9, selected_language="uz")
    show_menu_mock = AsyncMock()
    presentation_service = Mock()
    presentation_service.cancel_outline = AsyncMock()
    order_service = Mock()
    order_service.cancel_waiting_payment = AsyncMock()
    monkeypatch.setattr(
        presentation_handlers,
        "current_user",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(presentation_handlers, "show_main_menu", show_menu_mock)
    monkeypatch.setattr(
        presentation_handlers,
        "PresentationService",
        Mock(return_value=presentation_service),
    )
    monkeypatch.setattr(
        presentation_handlers,
        "OrderService",
        Mock(return_value=order_service),
    )

    await presentation_handlers.cancel_flow(
        message,
        state,
        AsyncMock(spec=AsyncSession),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    presentation_service.cancel_outline.assert_not_awaited()
    order_service.cancel_waiting_payment.assert_not_awaited()
    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once_with("Faol jarayon topilmadi.")
    show_menu_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistent_cancel_button_cancels_queued_presentation_from_idle_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"presentation_id": 7, "language": "uz"}
    state.get_state.return_value = None
    presentation_service = Mock(cancel_outline=AsyncMock(return_value=True))
    order_service = Mock(cancel_waiting_payment=AsyncMock())
    menu = AsyncMock()
    session = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(
        presentation_handlers,
        "PresentationService",
        Mock(return_value=presentation_service),
    )
    monkeypatch.setattr(
        presentation_handlers,
        "OrderService",
        Mock(return_value=order_service),
    )
    monkeypatch.setattr(
        presentation_handlers,
        "current_user",
        AsyncMock(return_value=SimpleNamespace(id=9, selected_language="uz")),
    )
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.cancel_flow(
        message,
        state,
        session,
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    presentation_service.cancel_outline.assert_awaited_once_with(session, presentation_id=7)
    order_service.cancel_waiting_payment.assert_not_awaited()
    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once_with("Jarayon bekor qilindi.")
    menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_back_from_direction_returns_to_language_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "ru"}
    state.get_state.return_value = PresentationFlow.direction.state
    menu = AsyncMock()
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)
    monkeypatch.setattr(
        presentation_handlers,
        "current_user",
        AsyncMock(return_value=SimpleNamespace(id=9)),
    )

    await presentation_handlers.back_flow(
        message,
        state,
        AsyncMock(spec=AsyncSession),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    state.set_state.assert_awaited_once_with(PresentationFlow.language)
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "Выберите язык:"
    assert message.answer.await_args.kwargs["reply_markup"] is not None
    menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_from_waiting_payment_cancels_order_before_returning_to_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {
        "language": "uz",
        "order_id": 8,
        "topic": "AI",
        "slide_count": 12,
    }
    state.get_state.return_value = PresentationFlow.waiting_payment.state
    session = AsyncMock(spec=AsyncSession)
    order_service = Mock(cancel_waiting_payment=AsyncMock(return_value=True))
    menu = AsyncMock()
    monkeypatch.setattr(
        presentation_handlers,
        "OrderService",
        Mock(return_value=order_service),
    )
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.back_flow(
        message,
        state,
        session,
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    order_service.cancel_waiting_payment.assert_awaited_once_with(
        session,
        order_id=8,
        telegram_user_id=555,
    )
    state.set_data.assert_awaited_once_with({"language": "uz", "topic": "AI", "slide_count": 12})
    state.set_state.assert_awaited_once_with(PresentationFlow.plan)
    state.clear.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "Tarifni tanlang:"
    assert message.answer.await_args.kwargs["reply_markup"] is not None
    menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_from_waiting_payment_failure_preserves_fsm_and_reports_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "uz", "order_id": 8}
    state.get_state.return_value = PresentationFlow.waiting_payment.state
    order_service = Mock(
        cancel_waiting_payment=AsyncMock(side_effect=ValueError("payment is no longer cancellable"))
    )
    menu = AsyncMock()
    monkeypatch.setattr(
        presentation_handlers,
        "OrderService",
        Mock(return_value=order_service),
    )
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.back_flow(
        message,
        state,
        AsyncMock(spec=AsyncSession),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    state.set_data.assert_not_awaited()
    state.set_state.assert_not_awaited()
    state.clear.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "Jarayon allaqachon boshlangan yoki bu buyurtmani bekor qilib bo‘lmaydi."
    )
    menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_from_idle_queued_flow_preserves_presentation_cancel_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=555), answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"presentation_id": 7, "language": "uz"}
    state.get_state.return_value = None
    menu = AsyncMock()
    monkeypatch.setattr(
        presentation_handlers,
        "current_user",
        AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    monkeypatch.setattr(presentation_handlers, "show_main_menu", menu)

    await presentation_handlers.back_flow(
        message,
        state,
        AsyncMock(spec=AsyncSession),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    state.clear.assert_not_awaited()
    state.set_data.assert_not_awaited()
    menu.assert_awaited_once()


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

    edit_text.assert_awaited_once()
    assert edit_text.await_args is not None
    assert edit_text.await_args.args[0] == (
        "0% · ⏳ Buyurtma navbatga qo‘yildi. Holat shu xabarda yangilanadi."
    )
    cancel_markup = edit_text.await_args.kwargs["reply_markup"]
    assert cancel_markup.inline_keyboard[0][0].callback_data == "outline:cancel:7"
    service.approve_outline.assert_awaited_once_with(
        session,
        presentation_id=7,
        progress_chat_id=555,
        progress_message_id=1234,
    )
    state.set_data.assert_awaited_once_with({"presentation_id": 7, "language": "uz"})
    state.set_state.assert_awaited_once_with(None)
    state.clear.assert_not_awaited()
    answer_callback.assert_awaited_once_with()
    assert events == ["database", "telegram"]


@pytest.mark.asyncio
async def test_design_style_moves_to_additional_requirements() -> None:
    message = SimpleNamespace(text="Zamonaviy", answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "uz"}

    await presentation_handlers.design_style(message, state, Localizer.load_default())

    state.update_data.assert_awaited_once_with(design_style="modern")
    state.set_state.assert_awaited_once_with(PresentationFlow.additional_requirements)
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0].startswith("Qo‘shimcha talabni")


@pytest.mark.asyncio
async def test_two_character_topic_advances_the_fsm() -> None:
    message = SimpleNamespace(text="AI", answer=AsyncMock())
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"language": "en"}

    await presentation_handlers.topic(message, state, Localizer.load_default())

    state.update_data.assert_awaited_once_with(topic="AI")
    state.set_state.assert_awaited_once_with(PresentationFlow.purpose)


@pytest.mark.asyncio
async def test_authorized_context_uses_presentation_language_not_profile_language() -> None:
    callback: Any = SimpleNamespace(
        data="outline:edit:7",
        from_user=SimpleNamespace(id=555),
    )
    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {}
    row = Mock()
    row.one_or_none.return_value = (8, "en")
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = row

    result = await presentation_handlers._authorized_presentation_context(
        callback,
        state,
        session,
        "edit",
    )

    assert result == (7, 8, "en")
    statement = str(session.execute.await_args.args[0])
    assert "presentations.language" in statement
    assert "users.selected_language" not in statement
    state.update_data.assert_awaited_once_with(
        presentation_id=7,
        order_id=8,
        language="en",
        outline_action="edit",
    )


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
