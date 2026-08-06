import pytest

from modules.templates.catalog import TemplateAccessError, select_template


@pytest.mark.parametrize(
    ("direction", "expected_style", "accent"),
    [
        ("Tibbiyot", "medical", "#0F9D8A"),
        ("IT", "technology", "#7C3AED"),
        ("Biznes", "business", "#1E3A5F"),
        ("Tarix", "historical", "#8B5E3C"),
        ("Ta’lim", "academic", "#2563EB"),
    ],
)
def test_ai_template_selection_uses_direction_rules(
    direction: str,
    expected_style: str,
    accent: str,
) -> None:
    selection = select_template(direction=direction, requested_style="ai", plan_code="medium")

    assert selection.style == expected_style
    assert selection.palette["accent"] == accent
    assert selection.minimum_font_size >= 18


def test_premium_custom_template_is_not_available_to_medium() -> None:
    with pytest.raises(TemplateAccessError):
        select_template(direction="Startap", requested_style="luxury", plan_code="medium")


def test_premium_plus_can_use_custom_luxury_template() -> None:
    selection = select_template(
        direction="Startap",
        requested_style="luxury",
        plan_code="premium_plus",
    )

    assert selection.style == "luxury"
    assert selection.supports_speaker_notes is True
