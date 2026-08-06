from aiogram.filters import StateFilter
from apps.bot.fsm import PresentationFlow
from apps.bot.handlers.presentation import quick_presentation, router


def test_quick_command_handler_covers_idle_and_early_flow_states() -> None:
    handler = next(item for item in router.message.handlers if item.callback is quick_presentation)
    state_filter = next(
        item.callback for item in handler.filters if isinstance(item.callback, StateFilter)
    )

    assert state_filter.states == (
        None,
        PresentationFlow.language,
        PresentationFlow.direction,
    )
