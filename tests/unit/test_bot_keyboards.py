from apps.bot.handlers.common import main_menu_text
from apps.bot.keyboards import choices_keyboard, main_menu_keyboard, resolve_choice
from apps.bot.localization import Localizer
from apps.bot.options import KNOWLEDGE_LEVELS, PLAN_CODES, directions_for


def test_main_menu_contains_required_actions_and_admin_is_conditional() -> None:
    localizer = Localizer.load_default()

    user_keyboard = main_menu_keyboard(localizer, "uz", is_admin=False)
    admin_keyboard = main_menu_keyboard(localizer, "uz", is_admin=True)
    user_labels = {button.text for row in user_keyboard.keyboard for button in row}
    admin_labels = {button.text for row in admin_keyboard.keyboard for button in row}

    assert localizer.text("uz", "menu.new") in user_labels
    assert localizer.text("uz", "menu.help") in user_labels
    assert localizer.text("uz", "menu.admin") not in user_labels
    assert localizer.text("uz", "menu.admin") in admin_labels


def test_uzbek_owner_menu_uses_the_required_primary_labels() -> None:
    localizer = Localizer.load_default()
    keyboard = main_menu_keyboard(localizer, "uz", is_admin=False)
    labels = {button.text for row in keyboard.keyboard for button in row}

    assert {
        "🚀 Yangi prezentatsiya",
        "📂 Mening prezentatsiyalarim",
        "ℹ️ Yordam",
        "⚙️ Sozlamalar",
    } <= labels


def test_main_menu_has_a_clear_mobile_first_hierarchy() -> None:
    localizer = Localizer.load_default()

    keyboard = main_menu_keyboard(localizer, "uz", is_admin=False)
    rows = [[button.text for button in row] for row in keyboard.keyboard]

    assert rows == [
        [localizer.text("uz", "menu.new")],
        [localizer.text("uz", "menu.mine"), localizer.text("uz", "menu.plans")],
        [localizer.text("uz", "menu.free_demo"), localizer.text("uz", "menu.profile")],
        [localizer.text("uz", "menu.help"), localizer.text("uz", "menu.settings")],
    ]
    assert keyboard.input_field_placeholder == localizer.text("uz", "menu.input_placeholder")


def test_main_menu_text_is_a_personalized_status_dashboard() -> None:
    localizer = Localizer.load_default()

    text = main_menu_text(
        localizer,
        language="uz",
        first_name="Nodirbek <Admin>",
        presentations_count=7,
        free_demo_used=False,
    )

    assert "Presentation AI" in text
    assert "Nodirbek &lt;Admin&gt;" in text
    assert "7" in text
    assert localizer.text("uz", "menu.demo_available") in text


def test_all_required_directions_are_available_in_each_language() -> None:
    assert len(directions_for("uz")) == 30
    assert len(directions_for("ru")) == 30
    assert len(directions_for("en")) == 30
    assert "Tibbiyot" in directions_for("uz")


def test_choice_keyboard_localizes_labels_and_resolves_canonical_values() -> None:
    localizer = Localizer.load_default()
    keyboard = choices_keyboard(KNOWLEDGE_LEVELS, localizer, "ru")
    labels = {button.text for row in keyboard.keyboard for button in row}

    assert keyboard.one_time_keyboard is True
    assert "Начальный" in labels
    assert "beginner" not in labels
    assert resolve_choice("Начальный", KNOWLEDGE_LEVELS, localizer, "ru") == "beginner"
    assert resolve_choice("beginner", KNOWLEDGE_LEVELS, localizer, "ru") == "beginner"

    plan_keyboard = choices_keyboard(PLAN_CODES, localizer, "en")
    plan_labels = {button.text for row in plan_keyboard.keyboard for button in row}
    assert "Premium+" in plan_labels
    assert "premium_plus" not in plan_labels
