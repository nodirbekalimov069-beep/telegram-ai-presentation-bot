from apps.bot.fsm import ServiceFlow
from apps.bot.handlers.menu import _ARTIFACT_CAPTION_KEYS
from apps.bot.keyboards import (
    ai_services_keyboard,
    language_keyboard,
    main_menu_keyboard,
    service_confirmation_keyboard,
    service_field_keyboard,
)
from apps.bot.localization import Localizer
from modules.orders.domain import OrderStatus
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType
from modules.service_orders.dashboard import friendly_order_status, paginate_orders
from modules.service_orders.document_services import PRODUCTION_DOCUMENT_SERVICE_TYPES


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


def test_course_confirmation_is_explicit_prepare_and_token_scoped() -> None:
    localizer = Localizer.load_default()
    keyboard = service_confirmation_keyboard(
        token="course-token",
        language="uz",
        localizer=localizer,
        service_type=ServiceType.COURSE_WORK,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    submit = next(button for button in buttons if button.callback_data == "svs:course-token")
    assert submit.text == "✅ Tayyorlash"


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


def test_course_work_has_complete_uz_ru_en_locale_parity() -> None:
    localizer = Localizer.load_default()
    keys = (
        "service.course",
        "service.course.notice",
        "service.course.created",
        "service.field.subject",
        "service.field.topic",
        "service.field.sections",
        "service.field.length",
        "service.field.language",
        "service.field.requirements",
        "service.summary.sections",
        "orders.course_artifact_caption",
    )
    for language in ("uz", "ru", "en"):
        assert all(localizer.text(language, key, artifact="DOCX") for key in keys)


def test_summary_confirmation_is_explicit_prepare_and_token_scoped() -> None:
    localizer = Localizer.load_default()
    keyboard = service_confirmation_keyboard(
        token="summary-token",
        language="uz",
        localizer=localizer,
        service_type=ServiceType.SUMMARY,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    submit = next(button for button in buttons if button.callback_data == "svs:summary-token")
    assert submit.text == "✅ Tayyorlash"
    assert {button.text for button in buttons} >= {
        "✅ Tayyorlash",
        "✏️ O‘zgartirish",
        "⬅️ Ortga",
        "❌ Bekor qilish",
        "🏠 Bosh menyu",
    }


def test_summary_has_complete_uz_ru_en_locale_parity() -> None:
    localizer = Localizer.load_default()
    keys = (
        "service.summary",
        "service.summary.notice",
        "service.summary.created",
        "service.summary.failure",
        "service.field.source",
        "service.field.detail",
        "service.field.language",
        "service.field.requirements",
        "orders.summary_artifact_caption",
    )
    for language in ("uz", "ru", "en"):
        assert all(localizer.text(language, key, artifact="DOCX") != key for key in keys)
    uz_created = localizer.text("uz", "service.summary.created")
    assert "editable" not in uz_created.casefold()
    assert "valid" not in uz_created.casefold()


def test_write_text_confirmation_locales_and_exact_rollout_policy() -> None:
    localizer = Localizer.load_default()
    production = {
        ServiceType.ESSAY,
        ServiceType.INDEPENDENT_WORK,
        ServiceType.COURSE_WORK,
        ServiceType.SUMMARY,
        ServiceType.QUIZ,
        ServiceType.WRITE_TEXT,
        ServiceType.REWRITE,
        ServiceType.TRANSLATE,
        ServiceType.DOCUMENT,
    }
    assert {
        item for item, definition in SERVICE_CATALOG.items() if definition.production_ready
    } == production
    assert {
        item for item, definition in SERVICE_CATALOG.items() if not definition.production_ready
    } == set()
    keyboard = service_confirmation_keyboard(
        token="write-token",
        language="uz",
        localizer=localizer,
        service_type=ServiceType.WRITE_TEXT,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert (
        next(button for button in buttons if button.callback_data == "svs:write-token").text
        == "✅ Tayyorlash"
    )
    for language in ("uz", "ru", "en"):
        for key in (
            "service.write",
            "service.write_notice",
            "service.write_created",
            "service.field.topic",
            "service.field.text_type",
            "service.field.style",
            "service.field.length",
            "service.field.language",
            "service.field.requirements",
        ):
            assert localizer.text(language, key) != key


def test_universal_service_fsm_is_small_and_extensible() -> None:
    assert {state.state.split(":")[-1] for state in ServiceFlow} == {
        "collecting",
        "confirming",
    }


def test_rewrite_uz_ru_en_wizard_localization_parity() -> None:
    localizer = Localizer.load_default()
    expected_names = {
        "uz": "Matnni qayta yozish",
        "ru": "Переписать текст",
        "en": "Rewrite text",
    }
    keys = (
        "service.field.text",
        "service.field.mode",
        "option.simple",
        "option.formal",
        "option.academic",
        "option.shorten",
        "option.clarify",
        "service.rewrite_notice",
        "service.rewrite_created",
        "service.essay.submit",
        "service.edit",
        "common.back",
        "common.cancel",
        "common.main_menu",
    )
    for language, name in expected_names.items():
        assert localizer.text(language, "service.rewrite") == name
        for key in keys:
            assert localizer.text(language, key).strip()


def test_all_production_document_history_captions_have_uz_ru_en_parity() -> None:
    assert {item.value for item in _ARTIFACT_CAPTION_KEYS} == PRODUCTION_DOCUMENT_SERVICE_TYPES
    localizer = Localizer.load_default()
    for caption_key in _ARTIFACT_CAPTION_KEYS.values():
        for language in ("uz", "ru", "en"):
            assert localizer.text(language, caption_key, artifact="DOCX").strip()


def test_rewrite_confirmation_and_exact_next_rollout_policy() -> None:
    localizer = Localizer.load_default()
    production = {
        ServiceType.ESSAY,
        ServiceType.INDEPENDENT_WORK,
        ServiceType.COURSE_WORK,
        ServiceType.SUMMARY,
        ServiceType.QUIZ,
        ServiceType.WRITE_TEXT,
        ServiceType.REWRITE,
        ServiceType.TRANSLATE,
        ServiceType.DOCUMENT,
    }
    assert {
        item for item, definition in SERVICE_CATALOG.items() if definition.production_ready
    } == production
    assert {
        item for item, definition in SERVICE_CATALOG.items() if not definition.production_ready
    } == set()
    keyboard = service_confirmation_keyboard(
        token="rewrite-token",
        language="uz",
        localizer=localizer,
        service_type=ServiceType.REWRITE,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert next(
        button for button in buttons if button.callback_data == "svs:rewrite-token"
    ).text == ("✅ Tayyorlash")
    assert {button.text for button in buttons} >= {
        "✅ Tayyorlash",
        "✏️ O‘zgartirish",
        "⬅️ Ortga",
        "❌ Bekor qilish",
        "🏠 Bosh menyu",
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
