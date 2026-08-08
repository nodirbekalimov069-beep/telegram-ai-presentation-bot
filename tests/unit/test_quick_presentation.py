import zipfile
from pathlib import Path

import pytest
from apps.bot.quick_presentation import (
    PptxTextPdfConverter,
    QuickPresentationRequestError,
    _request_settings,
    parse_quick_request,
    select_plan_code,
)
from modules.presentations.pdf import validate_pdf


def test_parse_quick_uzbek_command_extracts_requested_slide_count_and_images() -> None:
    request = parse_quick_request(
        "Menga Round Up dasturini tuzish amallari haqida rasmlar bilan "
        "12 sahifali rangli slayd tayyorlab ber",
        language="uz",
    )

    assert request.slide_count == 12
    assert request.include_images is True
    assert request.colorful is True
    assert request.topic == "Round Up dasturini tuzish amallari"
    assert len(request.topic) <= 80


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("Сделай презентацию на 10 слайдов о машинном обучении", "ru", 10),
        ("Please create a 12-slide presentation about AI", "en", 12),
    ],
)
def test_parse_quick_request_understands_russian_and_english_slide_words(
    text: str,
    language: str,
    expected: int,
) -> None:
    assert parse_quick_request(text, language=language).slide_count == expected


def test_parse_quick_request_rejects_unpriced_nine_slide_gap() -> None:
    with pytest.raises(ValueError, match="1–8 or 10–30"):
        parse_quick_request("Menga 9 slayd tayyorla", language="uz")


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Menga 130 slayd tayyorla", "uz"),
        ("Сделай 130 слайдов о машинном обучении", "ru"),
        ("Create 130 slides about AI", "en"),
    ],
)
def test_parse_quick_request_never_truncates_oversized_slide_count(
    text: str,
    language: str,
) -> None:
    with pytest.raises(QuickPresentationRequestError, match="between 1 and 30"):
        parse_quick_request(text, language=language)


def test_parse_quick_request_understands_uzbek_slaydli_suffix() -> None:
    request = parse_quick_request(
        "Menga 12 slaydli sun’iy intellekt taqdimoti kerak",
        language="uz",
    )

    assert request.slide_count == 12
    for count in (9, 130):
        with pytest.raises(QuickPresentationRequestError):
            parse_quick_request(
                f"Menga {count} slaydli taqdimot tayyorla",
                language="uz",
            )


def test_quick_visual_preferences_map_only_to_supported_generation_settings() -> None:
    request = parse_quick_request(
        "Menga AI haqida rasmlar bilan 12 sahifali rangli slayd tayyorla",
        language="uz",
    )

    settings = _request_settings(request)

    assert settings["design_style"] == "creative"
    assert "vizual" in str(settings["purpose"]).casefold()
    assert {"color_style", "image_amount", "diagram", "file_format"}.isdisjoint(settings)


@pytest.mark.parametrize(
    ("slide_count", "expected"),
    [(8, "free_demo"), (10, "medium"), (12, "medium"), (18, "premium"), (25, "premium_plus")],
)
def test_select_plan_code_uses_backend_plan_ranges(
    slide_count: int,
    expected: str,
) -> None:
    assert select_plan_code(slide_count) == expected


@pytest.mark.asyncio
async def test_pptx_text_pdf_converter_creates_one_content_page_per_slide(
    tmp_path: Path,
) -> None:
    pptx_path = tmp_path / "input.pptx"
    with zipfile.ZipFile(pptx_path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Kirish</a:t><a:t>Asosiy fikr</a:t></p:sld>',
        )
        archive.writestr(
            "ppt/slides/slide2.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Xulosa</a:t><a:t>Natija</a:t></p:sld>',
        )

    output = await PptxTextPdfConverter().convert(pptx_path, tmp_path / "output.pdf")
    report = validate_pdf(output, expected_pages=2)

    assert report.is_valid
    assert output.stat().st_size > 1_000
