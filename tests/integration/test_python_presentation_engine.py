from pathlib import Path

import pytest

from modules.presentations.engine import (
    NodePresentationRenderer,
    build_engine_payload,
    validate_pptx,
)


def sample_outline() -> dict:
    return {
        "title": "AI va ta’lim",
        "slides": [
            {
                "number": 1,
                "title": "Kirish",
                "purpose": "Kontekst berish",
                "key_points": ["AI imkoniyatlari", "Ta’limdagi ehtiyoj"],
                "visual_type": "icon",
                "layout": "title_and_content",
                "sources": [],
            },
            {
                "number": 2,
                "title": "Xulosa",
                "purpose": "Asosiy fikrlarni yakunlash",
                "key_points": ["Mas’uliyatli joriy etish", "Natijani o‘lchash"],
                "visual_type": "bar_chart",
                "layout": "summary",
                "sources": ["https://www.unesco.org/en/digital-education/artificial-intelligence"],
            },
        ],
    }


def test_payload_matches_node_contract_without_inventing_chart_data() -> None:
    payload = build_engine_payload(
        title="AI va ta’lim",
        language="uz",
        direction="Ta’lim",
        design_style="ai",
        plan_code="premium",
        outline=sample_outline(),
    )

    assert payload["plan_code"] == "PREMIUM"
    assert len(payload["slides"]) == 2
    assert "chart" not in payload["slides"][1]
    assert payload["slides"][1]["sources"][0]["url"].startswith("https://")
    assert payload["slides"][0]["speaker_notes"]


def test_payload_omits_null_values_from_structured_layout() -> None:
    outline = sample_outline()
    outline["slides"][0]["special_layout"] = {
        "type": "process",
        "items": [
            {"label": "Birinchi", "description": None},
            {"label": "Ikkinchi", "description": "Tayyor"},
        ],
    }

    payload = build_engine_payload(
        title="AI va ta’lim",
        language="uz",
        direction="Ta’lim",
        design_style="ai",
        plan_code="premium",
        outline=outline,
    )

    assert payload["slides"][0]["layout"]["items"] == [
        {"label": "Birinchi"},
        {"label": "Ikkinchi", "description": "Tayyor"},
    ]


@pytest.mark.asyncio
async def test_python_adapter_renders_real_pptx_and_qa_counts_slides(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    renderer = NodePresentationRenderer(project_root / "apps" / "presentation_engine")
    output = tmp_path / "result.pptx"
    payload = build_engine_payload(
        title="AI va ta’lim",
        language="uz",
        direction="Ta’lim",
        design_style="ai",
        plan_code="premium",
        outline=sample_outline(),
    )

    await renderer.render(payload, output)
    report = validate_pptx(output, expected_slides=2)

    assert report.slide_count == 2
    assert report.is_valid
    assert output.stat().st_size > 10_000
