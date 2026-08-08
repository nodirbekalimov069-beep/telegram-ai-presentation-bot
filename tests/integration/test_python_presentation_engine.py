from pathlib import Path

import pytest

from modules.presentations.engine import (
    NodePresentationRenderer,
    PresentationRenderError,
    _secure_image_paths,
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


@pytest.mark.asyncio
async def test_python_adapter_rejects_image_outside_render_workdir(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    renderer = NodePresentationRenderer(project_root / "apps" / "presentation_engine")
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside_image = tmp_path / "outside.png"
    outside_image.write_bytes(b"not-an-image")
    payload = build_engine_payload(
        title="AI va ta’lim",
        language="uz",
        direction="Ta’lim",
        design_style="ai",
        plan_code="premium",
        outline=sample_outline(),
    )
    payload["slides"][0]["image_paths"] = [str(outside_image)]

    with pytest.raises(PresentationRenderError, match="outside render workdir"):
        await renderer.render(payload, workdir / "result.pptx")


@pytest.mark.parametrize(
    "signature",
    [
        b"icns" + b"\x00" * 32,
        b"\x00\x00\x00\x0cJXL \r\n\x87\n" + b"\x00" * 16,
        b"\x00\x00\x00\x08JXL \x00\x00\x00\x0cftypjxl ",
        *[
            b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 20
            for brand in (
                b"avif",
                b"avis",
                b"heic",
                b"heis",
                b"heix",
                b"heim",
                b"hevc",
                b"hevx",
                b"mif1",
                b"msf1",
            )
        ],
    ],
)
def test_image_guard_rejects_vulnerable_format_even_with_png_extension(
    tmp_path: Path,
    signature: bytes,
) -> None:
    disguised_image = tmp_path / "disguised.png"
    disguised_image.write_bytes(signature)
    payload = {"slides": [{"image_paths": [str(disguised_image)]}]}

    with pytest.raises(PresentationRenderError, match="unsupported image format"):
        _secure_image_paths(payload, render_root=tmp_path)


def test_image_guard_does_not_block_unaffected_iso_bmff_brand(tmp_path: Path) -> None:
    image = tmp_path / "benign.bin"
    image.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)

    secured = _secure_image_paths(
        {"slides": [{"image_paths": [str(image)]}]},
        render_root=tmp_path,
    )

    assert secured["slides"][0]["image_paths"] == [str(image.resolve())]


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda path: {"slides": ({"image_paths": [str(path)]},)},
        lambda path: {"slides": [{"image_paths": (str(path),)}]},
    ],
)
def test_image_guard_rejects_tuple_containers_before_json_serialization(
    tmp_path: Path,
    payload_factory,  # type: ignore[no-untyped-def]
) -> None:
    workdir = tmp_path / "render"
    workdir.mkdir()
    image = workdir / "unsafe.png"
    image.write_bytes(b"icns" + b"\x00" * 32)

    with pytest.raises(PresentationRenderError, match="must be a list"):
        _secure_image_paths(payload_factory(image), render_root=workdir)
