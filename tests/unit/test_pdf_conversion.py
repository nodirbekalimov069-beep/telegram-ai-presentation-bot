from pathlib import Path

import pytest
from pypdf import PdfWriter

from modules.presentations.pdf import LibreOfficeConverter, validate_pdf


def test_pdf_qa_validates_page_count_and_a4_size(tmp_path: Path) -> None:
    path = tmp_path / "deck.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595.28, height=841.89)
    writer.add_blank_page(width=595.28, height=841.89)
    with path.open("wb") as handle:
        writer.write(handle)

    report = validate_pdf(path, expected_pages=2)

    assert report.is_valid
    assert report.page_count == 2


def test_libreoffice_adapter_fails_fast_for_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LibreOfficeConverter(binary=tmp_path / "missing-soffice")
