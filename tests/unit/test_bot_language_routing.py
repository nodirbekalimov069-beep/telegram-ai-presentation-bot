from aiogram.filters import StateFilter
from apps.bot.handlers.start import router, select_language


def test_onboarding_language_callback_does_not_capture_active_flows() -> None:
    handler = next(
        item for item in router.callback_query.handlers if item.callback is select_language
    )
    state_filters = [
        item.callback for item in handler.filters if isinstance(item.callback, StateFilter)
    ]

    assert len(state_filters) == 1
    assert state_filters[0].states == (None,)
