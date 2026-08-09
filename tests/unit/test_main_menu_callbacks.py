from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.bot.handlers import fallback as fallback_handlers
from apps.bot.handlers import menu as menu_handlers
from apps.bot.handlers import services as service_handlers
from apps.bot.handlers import start as start_handlers
from apps.bot.handlers.menu import _as_utc, _parse_orders_callback
from apps.bot.localization import Localizer
from core.config import Settings


@pytest.mark.asyncio
async def test_start_registers_user_clears_state_and_shows_main_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(selected_language="uz")
    register = AsyncMock(return_value=user)
    show = AsyncMock()
    monkeypatch.setattr(
        start_handlers, "UserService", Mock(return_value=SimpleNamespace(register=register))
    )
    monkeypatch.setattr(start_handlers, "show_main_menu", show)
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=777,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    )
    state = SimpleNamespace(clear=AsyncMock())
    session = AsyncMock()
    settings = Settings(app_env="test")
    localizer = Localizer.load_default()

    await start_handlers.start(
        message,
        state,  # type: ignore[arg-type]
        session,
        localizer,
        settings,
    )

    register.assert_awaited_once_with(
        session,
        telegram_user_id=777,
        username="owner",
        first_name="Owner",
        last_name=None,
        language_code="uz",
    )
    state.clear.assert_awaited_once_with()
    show.assert_awaited_once_with(
        message,
        user=user,
        localizer=localizer,
        settings=settings,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["how_bot", "presentation", "ai", "payment", "support"])
async def test_each_help_callback_returns_user_facing_text(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    monkeypatch.setattr(
        menu_handlers,
        "current_user",
        AsyncMock(return_value=SimpleNamespace(selected_language="uz")),
    )
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data=f"help:{code}",
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    state = SimpleNamespace(clear=AsyncMock())
    await menu_handlers.help_detail(
        callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
    )

    state.clear.assert_awaited_once_with()
    callback.answer.assert_awaited_once_with()
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert all(word not in text for word in ("Docker", "PostgreSQL", "Redis", "FSM", "JSON"))


@pytest.mark.asyncio
async def test_home_callback_clears_any_flow_and_renders_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    user = SimpleNamespace(selected_language="uz")
    monkeypatch.setattr(menu_handlers, "current_user", AsyncMock(return_value=user))
    show = AsyncMock()
    monkeypatch.setattr(menu_handlers, "show_main_menu", show)
    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    settings = Settings(app_env="test")

    await menu_handlers.callback_main_menu(
        callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
        settings,
    )

    state.clear.assert_awaited_once_with()
    show.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_cancel_is_token_scoped_and_returns_to_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    user = SimpleNamespace(selected_language="uz")
    monkeypatch.setattr(service_handlers, "current_user", AsyncMock(return_value=user))
    show = AsyncMock()
    monkeypatch.setattr(service_handlers, "show_main_menu", show)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"flow_token": "active123", "language": "uz"}),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(answer=AsyncMock())
    stale = SimpleNamespace(
        data="svx:stale456",
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    active = SimpleNamespace(
        data="svx:active123",
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    await service_handlers.service_cancel(
        stale,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
        Settings(app_env="test"),
    )
    state.clear.assert_not_awaited()
    await service_handlers.service_cancel(
        active,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
        Settings(app_env="test"),
    )

    state.clear.assert_awaited_once_with()
    show.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_callback_uses_saved_user_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fallback_handlers,
        "find_user",
        AsyncMock(return_value=SimpleNamespace(selected_language="ru")),
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=777, language_code="uz"),
    )
    await fallback_handlers.unknown_callback(
        callback,
        AsyncMock(),
        Localizer.load_default(),
    )
    callback.answer.assert_awaited_once_with("Эта кнопка устарела. Выберите сервис заново.")


@pytest.mark.asyncio
async def test_presentation_pricing_without_active_plans_is_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    user = SimpleNamespace(selected_language="uz", telegram_user_id=777)
    monkeypatch.setattr(menu_handlers, "current_user", AsyncMock(return_value=user))
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="price:presentation",
        message=message,
        from_user=SimpleNamespace(id=user.telegram_user_id),
        answer=AsyncMock(),
    )

    state = SimpleNamespace(clear=AsyncMock())
    await menu_handlers.pricing_category(
        callback,
        state,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        Localizer.load_default(),
    )

    state.clear.assert_awaited_once_with()
    text = message.answer.await_args.args[0]
    assert "Narxlar tayyorlanmoqda." in text
    assert "0 UZS" not in text


def test_free_demo_status_mapper_hides_internal_states() -> None:
    user = SimpleNamespace(free_demo_used=False)
    assert menu_handlers._free_demo_status_key(user, None) == "demo.available"
    assert (
        menu_handlers._free_demo_status_key(
            user, SimpleNamespace(status="RESERVED", attempt_count=1)
        )
        == "demo.reserved"
    )
    assert (
        menu_handlers._free_demo_status_key(
            user, SimpleNamespace(status="CONSUMED", attempt_count=1)
        )
        == "demo.used"
    )
    assert (
        menu_handlers._free_demo_status_key(
            user,
            SimpleNamespace(status="RELEASED", attempt_count=menu_handlers.MAX_FREE_DEMO_ATTEMPTS),
        )
        == "demo.used"
    )


@pytest.mark.asyncio
async def test_settings_interrupts_active_flow_and_language_uses_active_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    user = SimpleNamespace(selected_language="ru", notifications_enabled=True)
    monkeypatch.setattr(menu_handlers, "current_user", AsyncMock(return_value=user))
    message = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=777),
    )
    state = SimpleNamespace(clear=AsyncMock())
    session = AsyncMock()
    localizer = Localizer.load_default()

    await menu_handlers.settings_menu(
        message,
        state,  # type: ignore[arg-type]
        session,
        localizer,
    )
    state.clear.assert_awaited_once_with()

    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    state.clear.reset_mock()
    await menu_handlers.settings_language(
        callback,
        state,  # type: ignore[arg-type]
        session,
        localizer,
    )
    state.clear.assert_awaited_once_with()

    sent_keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert sent_keyboard.inline_keyboard[-1][0].text == "🏠 Главное меню"


@pytest.mark.asyncio
async def test_notification_callback_persists_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Transaction:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return None

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    user = SimpleNamespace(
        selected_language="uz",
        notifications_enabled=True,
    )
    session = SimpleNamespace(
        begin=lambda: Transaction(),
        scalar=AsyncMock(return_value=user),
    )
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    state = SimpleNamespace(clear=AsyncMock())
    await menu_handlers.settings_notifications(
        callback,
        state,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        Localizer.load_default(),
    )

    state.clear.assert_awaited_once_with()
    assert user.notifications_enabled is False
    callback.answer.assert_awaited_once_with()
    assert "Sozlama saqlandi." in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_orders_callback_clears_active_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    state = SimpleNamespace(clear=AsyncMock())
    callback = SimpleNamespace(
        data="orders:not-a-page",
        message=SimpleNamespace(answer=AsyncMock()),
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    await menu_handlers.orders_page(
        callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
    )

    state.clear.assert_awaited_once_with()
    callback.answer.assert_awaited_once_with()


def test_naive_database_watermark_is_encoded_as_utc() -> None:
    naive = datetime(2026, 1, 15, 12, 6)
    assert _as_utc(naive) == datetime(2026, 1, 15, 12, 6, tzinfo=UTC)


def test_orders_callback_parser_rejects_unbounded_or_invalid_values() -> None:
    assert _parse_orders_callback("orders:" + "9" * 10_000) is None
    assert _parse_orders_callback("orders:1:" + "9" * 10_000 + ":0:1") is None
    assert _parse_orders_callback("orders:1:1768507560000000:2:1") is None
    assert _parse_orders_callback("orders:1:1768507560000000:0:0") is None
    parsed = _parse_orders_callback("orders:1:1768478760000000:1:6")
    assert parsed == (1, (datetime(2026, 1, 15, 12, 6, tzinfo=UTC), 1, 6))
