from apps.bot.fsm import PresentationFlow
from apps.bot.localization import Localizer

REQUIRED_MENU_KEYS = {
    "menu.new",
    "menu.mine",
    "menu.plans",
    "menu.free_demo",
    "menu.profile",
    "menu.settings",
    "menu.help",
    "menu.dashboard",
    "menu.demo_available",
    "menu.demo_used",
    "menu.friend",
    "menu.input_placeholder",
    "common.back",
    "common.cancel",
    "common.main_menu",
}


def test_all_locales_have_identical_keys() -> None:
    localizer = Localizer.load_default()

    assert set(localizer.languages) == {"uz", "ru", "en"}
    uz_keys = set(localizer.catalog("uz"))
    assert uz_keys >= REQUIRED_MENU_KEYS
    assert set(localizer.catalog("ru")) == uz_keys
    assert set(localizer.catalog("en")) == uz_keys


def test_unknown_locale_falls_back_to_uzbek() -> None:
    localizer = Localizer.load_default()

    assert localizer.text("de", "menu.new") == localizer.text("uz", "menu.new")


def test_presentation_fsm_contains_complete_input_and_approval_flow() -> None:
    state_names = {state.state.split(":")[-1] for state in PresentationFlow}

    assert {
        "language",
        "direction",
        "topic",
        "purpose",
        "audience",
        "knowledge_level",
        "slide_count",
        "design_style",
        "color_style",
        "image_amount",
        "diagram",
        "file_format",
        "plan",
        "waiting_payment",
        "outline_review",
        "outline_edit",
        "queued",
        "rating",
    } <= state_names
