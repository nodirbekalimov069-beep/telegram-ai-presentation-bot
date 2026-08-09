from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document

from modules.ai.providers.mock import MockAIProvider
from modules.ai.essay_contracts import EssayGenerationRequest
from modules.service_orders.documents import (
    DocumentTooLarge,
    EssayDocumentRenderer,
    UnsafeDocument,
    validate_docx_security,
)


async def _essay():
    return await MockAIProvider().generate_essay(
        EssayGenerationRequest(
            topic="O‘zbekiston ta’limida sun’iy intellekt / ../../secret",
            language="uz",
            length="short",
            additional_requirements="O‘zbek apostroflari: o‘, g‘ va kirill мисол",
        )
    )


async def test_renderer_creates_openable_professional_docx(tmp_path: Path) -> None:
    essay = await _essay()

    output = EssayDocumentRenderer(font_name="Liberation Serif").render(
        essay, tmp_path, order_id=42
    )

    assert output == tmp_path / "referat_42.docx"
    assert output.is_file()
    document = Document(output)
    section = document.sections[0]
    assert abs(section.page_width.cm - 21.0) < 0.1
    assert abs(section.page_height.cm - 29.7) < 0.1
    assert abs(section.left_margin.cm - 2.5) < 0.01
    assert abs(section.right_margin.cm - 2.0) < 0.01
    headings = [p.text for p in document.paragraphs if p.style.name.startswith("Heading")]
    assert "Kirish" in headings
    assert "Xulosa" in headings
    assert "Foydalanilgan adabiyotlar" in headings
    corpus = "\n".join(p.text for p in document.paragraphs)
    assert essay.introduction in corpus
    assert essay.sections[0].paragraphs[0] in corpus
    assert "o‘, g‘" in corpus

    with ZipFile(output) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        footer_xml = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("word/footer") and name.endswith(".xml")
        )
    assert "TOC" in document_xml
    assert "PAGEREF" not in document_xml  # Word updates the generated TOC on open.
    assert "PAGE" in footer_xml


async def test_renderer_deletes_artifact_exceeding_limit(tmp_path: Path) -> None:
    essay = await _essay()
    renderer = EssayDocumentRenderer(max_output_bytes=100)

    with pytest.raises(DocumentTooLarge):
        renderer.render(essay, tmp_path, order_id=7)

    assert not (tmp_path / "referat_7.docx").exists()


def test_renderer_rejects_invalid_order_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EssayDocumentRenderer().output_path(tmp_path, order_id=0)


async def test_renderer_states_honestly_when_verified_references_are_absent(
    tmp_path: Path,
) -> None:
    essay = (await _essay()).model_copy(update={"references": []})

    output = EssayDocumentRenderer().render(essay, tmp_path, order_id=78)
    text = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)

    assert "Tekshirilgan bibliografik manbalar taqdim etilmadi." in text


@pytest.mark.asyncio
async def test_generated_docx_passes_fail_closed_ooxml_security_validation(
    tmp_path: Path,
) -> None:
    path = EssayDocumentRenderer().render(await _essay(), tmp_path, order_id=7)

    validate_docx_security(path)


@pytest.mark.parametrize(
    ("member_name", "content"),
    [
        ("word/vbaProject.bin", b"macro"),
        ("word/_rels/document.xml.rels", b'<Relationship TargetMode="External"/>'),
        ("../escape.txt", b"escape"),
    ],
)
def test_docx_security_rejects_macros_external_links_and_zip_traversal(
    tmp_path: Path, member_name: str, content: bytes
) -> None:
    path = tmp_path / "unsafe.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"types")
        archive.writestr("word/document.xml", b"document")
        archive.writestr(member_name, content)

    with pytest.raises(UnsafeDocument):
        validate_docx_security(path)


def test_docx_security_rejects_non_xml_required_opc_parts(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.docx"
    with ZipFile(malformed, "w") as archive:
        archive.writestr("[Content_Types].xml", b"not xml")
        archive.writestr("_rels/.rels", b"not xml")
        archive.writestr("word/document.xml", b"not xml")
    with pytest.raises(UnsafeDocument):
        validate_docx_security(malformed)


def test_docx_security_rejects_aliased_macro_content_type(tmp_path: Path) -> None:
    macro = tmp_path / "aliased-macro.docx"
    content_types = b"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/active.dat" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>"""
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"""
    document_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>"""
    with ZipFile(macro, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/active.dat", b"active")
    with pytest.raises(UnsafeDocument):
        validate_docx_security(macro)


def _write_minimal_docx(path: Path, root_relationships: bytes) -> None:
    content_types = b"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""
    document_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("word/document.xml", document_xml)


def test_docx_security_rejects_forged_office_relationship_type(tmp_path: Path) -> None:
    path = tmp_path / "forged-rel.docx"
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="https://evil.example/officeDocument" Target="word/document.xml"/></Relationships>"""
    _write_minimal_docx(path, relationships)
    with pytest.raises(UnsafeDocument):
        validate_docx_security(path)


def test_docx_security_rejects_qualified_target_mode_bypass(tmp_path: Path) -> None:
    path = tmp_path / "qualified-target-mode.docx"
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships" xmlns:x="urn:evil"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml" x:TargetMode="External"/></Relationships>"""
    _write_minimal_docx(path, relationships)
    with pytest.raises(UnsafeDocument):
        validate_docx_security(path)


def test_docx_security_rejects_equivalent_zip_alias(tmp_path: Path) -> None:
    path = tmp_path / "zip-alias.docx"
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"""
    _write_minimal_docx(path, relationships)
    with ZipFile(path, "a") as archive:
        archive.writestr("word/./shadow.xml", b"<shadow/>")
    with pytest.raises(UnsafeDocument):
        validate_docx_security(path)
