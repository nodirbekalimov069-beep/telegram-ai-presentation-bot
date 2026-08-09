"""Professional, deterministic DOCX rendering for academic essays."""

from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Mm, Pt
from docx.text.paragraph import Paragraph

from modules.ai.essay_contracts import EssayDocument


class DocumentTooLarge(RuntimeError):
    pass


class UnsafeDocument(ValueError):
    """Raised when a DOCX package violates the fail-closed OOXML policy."""


_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORDPROCESSING_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


def validate_docx_security(
    path: Path,
    *,
    max_entries: int = 512,
    max_uncompressed_bytes: int = 50_000_000,
) -> None:
    """Reject traversal, macros, external relationships and ZIP bombs."""

    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > max_entries:
                raise UnsafeDocument("DOCX entry count is outside the allowed range")
            total_size = 0
            names: set[str] = set()
            normalized_names: set[str] = set()
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                if not name or name.startswith(("/", "\\")) or "\\" in name or ".." in parts:
                    raise UnsafeDocument("DOCX contains an unsafe ZIP path")
                normalized = str(PurePosixPath(name))
                if normalized != name or "%" in name:
                    raise UnsafeDocument("DOCX contains a non-canonical ZIP path")
                folded = normalized.casefold()
                if folded in normalized_names:
                    raise UnsafeDocument("DOCX contains equivalent ZIP member aliases")
                normalized_names.add(folded)
                total_size += member.file_size
                if total_size > max_uncompressed_bytes:
                    raise UnsafeDocument("DOCX uncompressed size exceeds the limit")
                names.add(name)
            if len(names) != len(members):
                raise UnsafeDocument("Duplicate DOCX ZIP members are forbidden")
            if not {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}.issubset(names):
                raise UnsafeDocument("DOCX is missing required OOXML parts")
            try:
                content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
                document_xml = ElementTree.fromstring(archive.read("word/document.xml"))
                root_relationships = ElementTree.fromstring(archive.read("_rels/.rels"))
            except ElementTree.ParseError as exc:
                raise UnsafeDocument("DOCX required XML is invalid") from exc
            if content_types.tag != f"{{{_CONTENT_TYPES_NS}}}Types":
                raise UnsafeDocument("DOCX content-types root is invalid")
            overrides = {
                element.attrib.get("PartName"): element.attrib.get("ContentType")
                for element in content_types
                if element.tag == f"{{{_CONTENT_TYPES_NS}}}Override"
            }
            content_type_values = [
                element.attrib.get("ContentType", "") for element in content_types
            ]
            if any(
                "macroenabled" in value.lower() or "vbaproject" in value.lower()
                for value in content_type_values
            ):
                raise UnsafeDocument("Macro-enabled DOCX content types are forbidden")
            if overrides.get("/word/document.xml") != (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            ):
                raise UnsafeDocument("DOCX main document content type is invalid")
            if document_xml.tag != f"{{{_WORDPROCESSING_NS}}}document":
                raise UnsafeDocument("DOCX main document XML root is invalid")
            if root_relationships.tag != f"{{{_PACKAGE_RELATIONSHIPS_NS}}}Relationships":
                raise UnsafeDocument("DOCX root relationships namespace is invalid")
            office_targets = [
                relationship.attrib.get("Target")
                for relationship in root_relationships
                if relationship.tag == f"{{{_PACKAGE_RELATIONSHIPS_NS}}}Relationship"
                and relationship.attrib.get("Type") == _OFFICE_DOCUMENT_REL
                and relationship.attrib.get("TargetMode", "").lower() != "external"
            ]
            if office_targets != ["word/document.xml"]:
                raise UnsafeDocument("DOCX root relationship is invalid")
            if any(name.lower().endswith(".bin") for name in names):
                raise UnsafeDocument("Macro-enabled DOCX content is forbidden")
            for name in names:
                if not name.endswith(".rels"):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError as exc:
                    raise UnsafeDocument("DOCX relationship XML is invalid") from exc
                if root.tag != f"{{{_PACKAGE_RELATIONSHIPS_NS}}}Relationships":
                    raise UnsafeDocument("DOCX relationship namespace is invalid")
                for relationship in root.iter():
                    if relationship is root:
                        continue
                    if relationship.tag != f"{{{_PACKAGE_RELATIONSHIPS_NS}}}Relationship":
                        raise UnsafeDocument("DOCX relationship element is invalid")
                    if any("}" in key for key in relationship.attrib):
                        raise UnsafeDocument("Qualified DOCX relationship attributes are forbidden")
                    relationship_type = relationship.attrib.get("Type", "").lower()
                    target = relationship.attrib.get("Target", "").lower()
                    if "vbaproject" in relationship_type or target.endswith(".bin"):
                        raise UnsafeDocument("Macro DOCX relationships are forbidden")
                    target_mode = next(
                        (
                            value
                            for key, value in relationship.attrib.items()
                            if key.rsplit("}", 1)[-1] == "TargetMode"
                        ),
                        "",
                    )
                    if target_mode.lower() == "external":
                        raise UnsafeDocument("External DOCX relationships are forbidden")
    except (BadZipFile, OSError) as exc:
        raise UnsafeDocument("DOCX package cannot be safely opened") from exc


_LABELS = {
    "uz": {
        "kind": "REFERAT",
        "contents": "Mundarija",
        "introduction": "Kirish",
        "conclusion": "Xulosa",
        "references": "Foydalanilgan adabiyotlar",
        "reference_notice": "Tekshirilgan bibliografik manbalar taqdim etilmadi.",
        "toc_placeholder": "Word/LibreOffice: mundarijani yangilang",
    },
    "ru": {
        "kind": "РЕФЕРАТ",
        "contents": "Содержание",
        "introduction": "Введение",
        "conclusion": "Заключение",
        "references": "Список литературы",
        "reference_notice": "Проверенные библиографические источники не предоставлены.",
        "toc_placeholder": "Word/LibreOffice: обновите содержание",
    },
    "en": {
        "kind": "ACADEMIC ESSAY",
        "contents": "Contents",
        "introduction": "Introduction",
        "conclusion": "Conclusion",
        "references": "References",
        "reference_notice": "No verified bibliographic sources were supplied.",
        "toc_placeholder": "Word/LibreOffice: update the table of contents",
    },
}


class EssayDocumentRenderer:
    def __init__(
        self,
        *,
        font_name: str = "Noto Serif",
        max_output_bytes: int = 20_000_000,
    ) -> None:
        if not font_name.strip() or len(font_name) > 100:
            raise ValueError("invalid document font")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self._font_name = font_name.strip()
        self._max_output_bytes = max_output_bytes

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"referat_{order_id}.docx"

    def render(self, essay: EssayDocument, output_dir: Path, *, order_id: int) -> Path:
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure_document(document)
        labels = _LABELS[essay.language]
        self._add_title_page(document, essay, labels)
        self._add_contents(document, labels)
        self._add_heading(document, labels["introduction"], level=1)
        self._add_body_paragraph(document, essay.introduction)
        for section in essay.sections:
            self._add_heading(document, section.heading, level=1)
            for body_text in section.paragraphs:
                self._add_body_paragraph(document, body_text)
        self._add_heading(document, labels["conclusion"], level=1)
        self._add_body_paragraph(document, essay.conclusion)
        self._add_heading(document, labels["references"], level=1)
        notice = document.add_paragraph(labels["reference_notice"])
        notice.runs[0].italic = True
        for index, reference in enumerate(essay.references, start=1):
            paragraph = document.add_paragraph(
                f"{index}. {reference.description} — {reference.verification_note}"
            )
            paragraph.paragraph_format.space_after = Pt(6)
        self._enable_field_updates(document)
        document.save(str(output))
        try:
            validate_docx_security(output)
        except UnsafeDocument:
            output.unlink(missing_ok=True)
            raise
        if output.stat().st_size > self._max_output_bytes:
            output.unlink(missing_ok=True)
            raise DocumentTooLarge("DOCX exceeds configured artifact limit")
        return output

    def _configure_document(self, document: DocumentObject) -> None:
        section = document.sections[0]
        section.page_width = Mm(210)
        section.page_height = Mm(297)
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2)
        normal = document.styles["Normal"]
        normal.font.name = self._font_name
        normal.font.size = Pt(14)
        normal._element.rPr.rFonts.set(qn("w:eastAsia"), self._font_name)
        for style_name in ("Title", "Heading 1", "Heading 2"):
            style = document.styles[style_name]
            style.font.name = self._font_name
            style._element.rPr.rFonts.set(qn("w:eastAsia"), self._font_name)
        self._add_page_number(section.footer.paragraphs[0])

    def _add_title_page(
        self, document: DocumentObject, essay: EssayDocument, labels: dict[str, str]
    ) -> None:
        document.add_paragraph()
        kind = document.add_paragraph(labels["kind"])
        kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
        kind.runs[0].bold = True
        kind.runs[0].font.size = Pt(18)
        title = document.add_paragraph(essay.title, style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_before = Pt(72)
        title.paragraph_format.space_after = Pt(24)
        generated = document.add_paragraph("Presentation AI Nodirbek")
        generated.alignment = WD_ALIGN_PARAGRAPH.CENTER
        generated.runs[0].italic = True
        document.add_page_break()  # type: ignore[no-untyped-call]

    def _add_contents(self, document: DocumentObject, labels: dict[str, str]) -> None:
        self._add_heading(document, labels["contents"], level=1)
        paragraph = document.add_paragraph()
        run = paragraph.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instruction = OxmlElement("w:instrText")
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = ' TOC \\o "1-3" \\h \\z \\u '
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        placeholder = OxmlElement("w:t")
        placeholder.text = labels["toc_placeholder"]
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        for element in (begin, instruction, separate, placeholder, end):
            run._r.append(element)
        document.add_page_break()  # type: ignore[no-untyped-call]

    def _add_heading(self, document: DocumentObject, text: str, *, level: int) -> None:
        paragraph = document.add_heading(text, level=level)
        paragraph.paragraph_format.space_before = Pt(12)
        paragraph.paragraph_format.space_after = Pt(6)

    def _add_body_paragraph(self, document: DocumentObject, text: str) -> None:
        paragraph = document.add_paragraph(text)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.first_line_indent = Cm(1.25)
        paragraph.paragraph_format.line_spacing = 1.5
        paragraph.paragraph_format.space_after = Pt(0)

    @staticmethod
    def _add_page_number(paragraph: Paragraph) -> None:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instruction = OxmlElement("w:instrText")
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = " PAGE "
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        for element in (begin, instruction, end):
            run._r.append(element)

    @staticmethod
    def _enable_field_updates(document: DocumentObject) -> None:
        settings = document.settings.element
        update = OxmlElement("w:updateFields")
        update.set(qn("w:val"), "true")
        settings.append(update)
