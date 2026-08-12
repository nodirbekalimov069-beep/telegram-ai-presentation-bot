from typing import Any, cast

from pypdf import PdfReader

from scripts.local_translation_e2e import _artifact_bodies_are_nonempty, _pdf_body_text


class _Page:
    def extract_text(self) -> str:
        return (
            "Tarjima\n"
            "1. Ali 2026-08-12 gacha 25 USD to‘lashi kerak.\n"
            "2) user@example.com manziliga yozing."
        )


class _Reader:
    pages = [_Page()]


def test_pdf_parity_normalization_removes_only_rendered_list_markers() -> None:
    text = _pdf_body_text(cast(PdfReader, cast(Any, _Reader())))
    assert "1. Ali" not in text
    assert "2) user@example.com" not in text
    assert "2026-08-12" in text
    assert "25 USD" in text
    assert "user@example.com" in text


def test_short_translation_artifacts_are_meaningful_when_nonempty() -> None:
    assert _artifact_bodies_are_nonempty("Все кошки спят", "Tarjima Все кошки спят")
    assert not _artifact_bodies_are_nonempty("", "Tarjima")
