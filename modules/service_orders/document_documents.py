"""Professional editable DOCX renderer for source-bound DOCUMENT drafts."""

from pathlib import Path
import unicodedata

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from pypdf import PdfReader

from modules.ai.document_contracts import PreparedDocument, normalized_document_body
from modules.service_orders.documents import DocumentTooLarge, validate_docx_security


class DocumentDocumentRenderer:
    def __init__(
        self, *, font_name: str = "Noto Serif", max_output_bytes: int = 20_000_000
    ) -> None:
        if not font_name.strip() or len(font_name) > 100 or max_output_bytes < 1:
            raise ValueError("invalid document renderer limits")
        self._font_name = font_name.strip()
        self._max_output_bytes = max_output_bytes

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"hujjat_{order_id}.docx"

    def render(self, value: PreparedDocument, output_dir: Path, *, order_id: int) -> Path:
        canonical = PreparedDocument.model_validate(value.model_dump())
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure(document)

        title = document.add_paragraph(canonical.title)
        title.style = document.styles["Title"]
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_after = Pt(18)

        for unit in canonical.body_units:
            paragraph = document.add_paragraph(unit)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.first_line_indent = Cm(1.25)
            paragraph.paragraph_format.line_spacing = 1.5
            paragraph.paragraph_format.space_after = Pt(8)

        notice = document.add_paragraph(canonical.limitation_notice)
        notice.paragraph_format.space_before = Pt(18)
        notice.paragraph_format.space_after = Pt(6)
        for run in notice.runs:
            run.italic = True
            run.font.size = Pt(9)

        document.core_properties.title = canonical.title
        document.core_properties.subject = "DOCUMENT DRAFT"
        document.save(str(output))
        if not output.is_file() or output.stat().st_size > self._max_output_bytes:
            output.unlink(missing_ok=True)
            raise DocumentTooLarge("DOCUMENT DOCX exceeded artifact limit")
        validate_docx_security(output)
        if extracted_document_docx_body(output) != normalized_document_body(canonical):
            output.unlink(missing_ok=True)
            raise ValueError("DOCUMENT DOCX canonical body mismatch")
        return output

    def _configure(self, document: DocumentObject) -> None:
        section = document.sections[0]
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2)
        normal = document.styles["Normal"]
        normal.font.name = self._font_name
        normal.font.size = Pt(12)
        normal._element.rPr.rFonts.set(qn("w:eastAsia"), self._font_name)
        title = document.styles["Title"]
        title.font.name = self._font_name
        title._element.rPr.rFonts.set(qn("w:eastAsia"), self._font_name)


def extracted_document_docx_body(path: Path) -> str:
    document = Document(str(path))
    return " ".join(
        unicodedata.normalize(
            "NFKC", " ".join(paragraph.text for paragraph in document.paragraphs)
        ).split()
    )


def extracted_document_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path), strict=True)
    return " ".join(
        unicodedata.normalize(
            "NFKC", " ".join((page.extract_text() or "") for page in reader.pages)
        ).split()
    )


def document_artifact_parity_score(
    value: PreparedDocument, *, docx_path: Path, pdf_path: Path
) -> float:
    canonical = normalized_document_body(value)
    return (
        1.0
        if extracted_document_docx_body(docx_path) == canonical
        and extracted_document_pdf_text(pdf_path) == canonical
        else 0.0
    )
