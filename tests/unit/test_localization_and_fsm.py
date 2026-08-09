import ast
from pathlib import Path

from apps.bot.fsm import PresentationFlow
from apps.bot.localization import Localizer
from apps.bot.options import DESIGN_STYLES, KNOWLEDGE_LEVELS, PLAN_CODES

REQUIRED_MENU_KEYS = {
    "menu.new",
    "menu.ai_services",
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


def test_all_literal_localization_keys_used_by_apps_exist() -> None:
    localizer = Localizer.load_default()
    available = set(localizer.catalog("uz"))
    apps_root = Path(__file__).parents[2] / "apps"
    missing: list[str] = []

    for path in apps_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "text"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                continue
            key = node.args[1].value
            if key not in available:
                missing.append(f"{path.relative_to(apps_root)}:{node.lineno}:{key}")

    assert missing == []


def test_presentation_fsm_contains_complete_input_and_approval_flow() -> None:
    state_names = {state.state.split(":")[-1] for state in PresentationFlow}

    assert state_names == {
        "language",
        "direction",
        "topic",
        "purpose",
        "audience",
        "knowledge_level",
        "slide_count",
        "design_style",
        "additional_requirements",
        "plan",
        "waiting_payment",
        "outline_review",
        "outline_edit",
    }


def test_presentation_steps_copy_matches_actual_fsm_order() -> None:
    localizer = Localizer.load_default()
    assert localizer.text("uz", "create.steps").splitlines() == [
        "1. Til",
        "2. Yo‘nalish",
        "3. Mavzu",
        "4. Maqsad, auditoriya va bilim darajasi",
        "5. Slayd soni",
        "6. Dizayn/uslub",
        "7. Qo‘shimcha talab",
        "8. Tarif",
        "9. Rejani tasdiqlash",
        "10. Generatsiya",
    ]


def test_general_design_menu_does_not_offer_plan_locked_luxury_style() -> None:
    assert "luxury" not in DESIGN_STYLES


def test_all_canonical_reply_options_have_localized_labels() -> None:
    localizer = Localizer.load_default()
    values = (*KNOWLEDGE_LEVELS, *DESIGN_STYLES, *PLAN_CODES)

    for language in localizer.languages:
        catalog = localizer.catalog(language)
        assert all(f"option.{value}" in catalog for value in values)
