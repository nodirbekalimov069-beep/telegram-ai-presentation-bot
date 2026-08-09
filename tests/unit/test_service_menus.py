from apps.bot.fsm import ServiceFlow
from apps.bot.keyboards import (
    ai_services_keyboard,
    language_keyboard,
    main_menu_keyboard,
    service_field_keyboard,
)
from apps.bot.localization import Localizer
from modules.orders.domain import OrderStatus
from modules.service_orders.catalog import SERVICE_CATALOG
from modules.service_orders.dashboard import friendly_order_status, paginate_orders


def test_main_menu_matches_requested_service_dashboard() -> None:
    localizer = Localizer.load_default()
    keyboard = main_menu_keyboard(localizer, "uz", is_admin=False)
    rows = [[button.text for button in row] for row in keyboard.keyboard]

    assert rows == [
        ["🎨 Taqdimot yaratish"],
        ["🤖 AI xizmatlar", "📂 Mening buyurtmalarim"],
        ["💰 Narxlar", "🎁 Bepul sinab ko‘rish"],
        ["ℹ️ Yordam", "⚙️ Sozlamalar"],
    ]


def test_ai_services_keyboard_has_one_safe_callback_per_catalog_entry() -> None:
    keyboard = ai_services_keyboard("uz")
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    service_callbacks = [
        button.callback_data for button in buttons if button.callback_data.startswith("svc:")
    ]

    assert len(service_callbacks) == len(SERVICE_CATALOG) == 9
    assert len(service_callbacks) == len(set(service_callbacks))
    assert all(value is not None and len(value.encode()) <= 64 for value in service_callbacks)
    assert any(button.callback_data == "nav:home" for button in buttons)


def test_service_navigation_has_token_scoped_back_home_and_cancel() -> None:
    localizer = Localizer.load_default()
    token = "1234567890abcdef"
    field = next(iter(SERVICE_CATALOG.values())).fields[0]
    keyboard = service_field_keyboard(
        field,
        token=token,
        field_index=0,
        language="uz",
        localizer=localizer,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert {button.callback_data for button in buttons} == {
        f"svb:{token}",
        "nav:home",
        f"svx:{token}",
    }


def test_language_selection_can_return_home_in_active_locale() -> None:
    localizer = Localizer.load_default()
    keyboard = language_keyboard(localizer, "ru")
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert callbacks == {"lang:uz", "lang:ru", "lang:en", "nav:home"}
    assert keyboard.inline_keyboard[-1][0].text == "🏠 Главное меню"


def test_new_service_locales_are_real_translations() -> None:
    localizer = Localizer.load_default()
    assert localizer.text("en", "service.catalog.title") == "🤖 AI services"
    assert localizer.text("ru", "service.field.topic") == "Введите тему:"
    assert localizer.text("en", "orders.title") == "📂 My orders"
    assert localizer.text("ru", "settings.notifications") == "🔔 Уведомления"
    assert localizer.option_label("en", "creative") == "Creative"
    assert localizer.option_label("ru", "medium") == "Средний"


def test_universal_service_fsm_is_small_and_extensible() -> None:
    assert {state.state.split(":")[-1] for state in ServiceFlow} == {
        "collecting",
        "confirming",
    }


def test_user_friendly_statuses_hide_internal_state_names() -> None:
    assert friendly_order_status("PREVIEW", "uz") == "🧪 Xizmat tayyorlanmoqda"
    assert friendly_order_status("GENERATING", "uz") == "⏳ Tayyorlanmoqda"
    assert friendly_order_status("COMPLETED", "uz") == "✅ Tayyor"
    assert friendly_order_status("FAILED", "uz") == "❌ Xatolik"
    for status in OrderStatus:
        assert friendly_order_status(status.value, "uz") != "🕓 Holat yangilanmoqda"
    assert friendly_order_status("FUTURE_STATUS", "uz") == "🕓 Holat yangilanmoqda"
    assert friendly_order_status("QUEUED", "uz") == "🕓 Navbatda"


def test_order_pagination_is_bounded_and_deterministic() -> None:
    orders = [f"order-{index}" for index in range(13)]
    first = paginate_orders(orders, page=0, per_page=5)
    last = paginate_orders(orders, page=99, per_page=5)

    assert first.items == tuple(orders[:5])
    assert first.page == 0 and first.total_pages == 3
    assert last.items == tuple(orders[10:])
    assert last.page == 2 and last.total_pages == 3
