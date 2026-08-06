from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import StateFilter
from apps.bot.handlers import presentation as presentation_handlers
from apps.bot.handlers.presentation import quick_presentation, router
from apps.bot.localization import Localizer
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
