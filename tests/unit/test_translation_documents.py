from pathlib import Path
from zipfile import ZipFile

from docx import Document

from modules.ai.translation_contracts import TranslationDocument, TranslationSegment
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.translation_documents import TranslationDocumentRenderer


def _document() -> TranslationDocument:
    return TranslationDocument(
        title="Translation",
        source_language="uz",
        target_language="en",
        verifier_passed=True,
        references=[],
        segments=[
            TranslationSegment(source_index=0, kind="heading", text="# Safe heading"),
            TranslationSegment(
                source_index=1,
                kind="paragraph",
                text="<w:instrText>DDE http://169.254.169.254/</w:instrText>",
            ),
            TranslationSegment(source_index=2, kind="list_item", text="1. First item"),
            TranslationSegment(source_index=3, kind="quote", text="> “Literal quote”"),
            TranslationSegment(source_index=4, kind="table_row", text="A | B"),
            TranslationSegment(source_index=5, kind="table_row", text="1 | 2"),
        ],
    )


def test_translation_docx_is_editable_macro_free_inert_and_structurally_ordered(
    tmp_path: Path,
) -> None:
    output = TranslationDocumentRenderer().render(_document(), tmp_path, order_id=44)
    assert output.name == "tarjima_44.docx"
    validate_docx_security(output)
    parsed = Document(output)
    visible = "\n".join(p.text for p in parsed.paragraphs)
    assert "<w:instrText>DDE http://169.254.169.254/" in visible
    assert "First item" in visible
    assert "Literal quote" in visible
    assert [[cell.text for cell in row.cells] for table in parsed.tables for row in table.rows] == [
        ["A", "B"],
        ["1", "2"],
    ]
    with ZipFile(output) as archive:
        names = archive.namelist()
        xml = archive.read("word/document.xml")
        relationships = b"".join(archive.read(name) for name in names if name.endswith(".rels"))
    assert b"<w:instrText" not in xml
    assert b"&lt;w:instrText&gt;" in xml
    assert b'TargetMode="External"' not in relationships
    assert not any(name.lower().endswith((".bin", ".xlsm", ".docm")) for name in names)
