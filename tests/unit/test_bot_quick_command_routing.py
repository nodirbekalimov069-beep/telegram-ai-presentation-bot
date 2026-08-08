from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import StateFilter
from apps.bot.handlers import presentation as presentation_handlers
from apps.bot.handlers.presentation import quick_presentation, router
from apps.bot.localization import Localizer
from apps.bot.quick_presentation import QuickPresentationRequestError
from core.config import Settings


def test_quick_command_handler_only_covers_idle_state() -> None:
    handler = next(item for item in router.message.handlers if item.callback is quick_presentation)
    state_filter = next(
        item.callback for item in handler.filters if isinstance(item.callback, StateFilter)
    )

    assert state_filter.states == (None,)


@pytest.mark.asyncio
async def test_non_test_idle_text_starts_confirmed_wizard_instead_of_quick_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wizard = AsyncMock()
    quick_service = Mock()
    quick_service.run = AsyncMock()
    monkeypatch.setattr(presentation_handlers, "new_presentation", wizard)
    monkeypatch.setattr(
        presentation_handlers,
        "QuickPresentationService",
        Mock(return_value=quick_service),
    )
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), text="AI ta’limda haqida taqdimot")
    state = AsyncMock()
    session = AsyncMock()
    localizer = Localizer.load_default()

    await quick_presentation(
        message,
        state,
        session,
        AsyncMock(),
        localizer,
        Settings(app_env="development"),
    )

    wizard.assert_awaited_once_with(message, state, session, localizer)
    quick_service.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_quick_request_gets_localized_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        text="Menga 9 slayd tayyorla",
        answer=AsyncMock(return_value=progress),
    )
    state = AsyncMock()
    quick_service = Mock(
        run=AsyncMock(side_effect=QuickPresentationRequestError("unsupported slide count"))
    )
    monkeypatch.setattr(
        presentation_handlers,
        "QuickPresentationService",
        Mock(return_value=quick_service),
    )
    monkeypatch.setattr(
        presentation_handlers,
        "_language",
        AsyncMock(return_value="uz"),
    )

    await quick_presentation(
        message,
        state,
        AsyncMock(),
        AsyncMock(),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    progress.edit_text.assert_awaited_once_with("1–8 yoki 10–30 slayd sonini kiriting.")


@pytest.mark.asyncio
async def test_internal_value_error_keeps_technical_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        text="Menga 10 slayd tayyorla",
        answer=AsyncMock(return_value=progress),
    )
    quick_service = Mock(run=AsyncMock(side_effect=ValueError("renderer contract failed")))
    monkeypatch.setattr(
        presentation_handlers,
        "QuickPresentationService",
        Mock(return_value=quick_service),
    )
    monkeypatch.setattr(
        presentation_handlers,
        "_language",
        AsyncMock(return_value="uz"),
    )

    await quick_presentation(
        message,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    progress.edit_text.assert_awaited_once_with(
        "❌ Taqdimotni yaratishda texnik xatolik yuz berdi. Buyruqni qayta yuborishingiz mumkin."
    )
