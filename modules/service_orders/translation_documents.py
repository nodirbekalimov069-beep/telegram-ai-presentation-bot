"""Editable, inert DOCX renderer for verified TRANSLATE documents."""

from pathlib import Path
import re
import hashlib
import unicodedata

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from modules.ai.contracts import AIContractError
from modules.ai.translation_contracts import TranslationDocument
from modules.service_orders.documents import validate_docx_security

_LANGUAGE_LABELS = {"uz": "O‘zbekcha", "ru": "Русский", "en": "English"}


def normalized_translation_body(value: TranslationDocument) -> str:
    """Return the renderer's normalized visible body without retaining it in metadata."""

    parts: list[str] = []
    for segment in value.segments:
        text = segment.text
        if segment.kind == "heading":
            text = re.sub(r"^#{1,6}\s+", "", text)
        elif segment.kind == "list_item":
            text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text)
        elif segment.kind == "quote":
            text = re.sub(r"^\s*>\s?", "", text)
        elif segment.kind == "table_row":
            text = " ".join(cell.strip() for cell in text.split("|"))
        parts.append(text)
    return " ".join(unicodedata.normalize("NFKC", " ".join(parts)).split())


def translation_body_digest(value: TranslationDocument) -> str:
    return hashlib.sha256(normalized_translation_body(value).encode()).hexdigest()


def extracted_translation_docx_body(path: Path) -> str:
    document = Document(str(path))
    parts: list[str] = []
    leading_paragraphs = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if leading_paragraphs < 2:
                leading_paragraphs += 1
                continue
            parts.append(block.text)
        elif isinstance(block, Table):
            parts.extend(" ".join(cell.text.strip() for cell in row.cells) for row in block.rows)
    return " ".join(unicodedata.normalize("NFKC", " ".join(parts)).split())


def extracted_translation_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path), strict=True)
    lines: list[str] = []
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            lines.append(re.sub(r"^\s*(?:[-*•]|\d{1,4}[.)])\s+", "", line))
    return " ".join(unicodedata.normalize("NFKC", " ".join(lines)).split())


def translation_artifact_parity_score(
    value: TranslationDocument, *, docx_path: Path, pdf_path: Path
) -> float:
    canonical = normalized_translation_body(value)
    docx_body = extracted_translation_docx_body(docx_path)
    pdf_text = extracted_translation_pdf_text(pdf_path)
    return 1.0 if docx_body == canonical and canonical in pdf_text else 0.0


class TranslationDocumentRenderer:
    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"tarjima_{order_id}.docx"

    def render(self, value: TranslationDocument, output_dir: Path, *, order_id: int) -> Path:
        canonical = TranslationDocument.model_validate(value.model_dump())
        if not canonical.verifier_passed or canonical.references:
            raise AIContractError("only a risk-gated reference-free translation may be rendered")
        target = self.output_path(output_dir, order_id=order_id)
        target.unlink(missing_ok=True)
        document = Document()
        section = document.sections[0]
        section.top_margin = section.bottom_margin = Inches(0.8)
        section.left_margin = section.right_margin = Inches(0.9)
        document.styles["Normal"].font.name = "Aptos"
        document.styles["Normal"].font.size = Pt(11)
        title = document.add_heading(canonical.title, level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta = document.add_paragraph(
            f"{_LANGUAGE_LABELS[canonical.source_language]} → "
            f"{_LANGUAGE_LABELS[canonical.target_language]}"
        )
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER

        index = 0
        while index < len(canonical.segments):
            segment = canonical.segments[index]
            if segment.kind == "table_row":
                rows: list[list[str]] = []
                while (
                    index < len(canonical.segments)
                    and canonical.segments[index].kind == "table_row"
                ):
                    rows.append(
                        [cell.strip() for cell in canonical.segments[index].text.split("|")]
                    )
                    index += 1
                columns = len(rows[0])
                if columns < 2 or any(len(row) != columns for row in rows):
                    raise AIContractError("translation table cell counts are inconsistent")
                table = document.add_table(rows=len(rows), cols=columns)
                table.style = "Table Grid"
                for row_number, values in enumerate(rows):
                    for column_number, text in enumerate(values):
                        table.cell(row_number, column_number).text = text
                continue
            if segment.kind == "heading":
                text = re.sub(r"^#{1,6}\s+", "", segment.text)
                paragraph = document.add_heading(text, level=1)
            elif segment.kind == "list_item":
                numbered = bool(re.match(r"^\s*\d+[.)]\s+", segment.text))
                text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", segment.text)
                paragraph = document.add_paragraph(
                    text, style="List Number" if numbered else "List Bullet"
                )
            elif segment.kind == "quote":
                paragraph = document.add_paragraph(re.sub(r"^\s*>\s?", "", segment.text))
                paragraph.paragraph_format.left_indent = Inches(0.35)
                for run in paragraph.runs:
                    run.italic = True
            else:
                paragraph = document.add_paragraph(segment.text)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.space_after = Pt(8)
            index += 1
        document.core_properties.title = canonical.title
        document.core_properties.subject = "TRANSLATE"
        document.save(str(target))
        validate_docx_security(target)
        return target
