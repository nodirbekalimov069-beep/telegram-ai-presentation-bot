"""Editable DOCX renderer for strict source-bound REWRITE documents."""

from pathlib import Path
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from modules.ai.contracts import AIContractError
from modules.ai.rewrite_contracts import RewriteDocument
from modules.service_orders.documents import validate_docx_security

_MODE_LABELS = {
    "uz": {
        "simple": "Sodda",
        "formal": "Rasmiy",
        "academic": "Akademik",
        "shorten": "Qisqartirilgan",
        "clarify": "Aniqlashtirilgan",
    },
    "ru": {
        "simple": "Простой",
        "formal": "Официальный",
        "academic": "Академический",
        "shorten": "Сокращённый",
        "clarify": "Уточнённый",
    },
    "en": {
        "simple": "Simple",
        "formal": "Formal",
        "academic": "Academic",
        "shorten": "Shortened",
        "clarify": "Clarified",
    },
}


class RewriteDocumentRenderer:
    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"qayta_yozish_{order_id}.docx"

    def render(self, value: RewriteDocument, output_dir: Path, *, order_id: int) -> Path:
        document_value = RewriteDocument.model_validate(value.model_dump())
        body = "\n".join(segment.text for segment in document_value.segments)
        if len(body) < 20 or any(not segment.text.strip() for segment in document_value.segments):
            raise AIContractError("rewrite artifact content is too short or empty")
        if document_value.references:
            raise AIContractError("rewrite artifact must not contain unverified references")

        target = self.output_path(output_dir, order_id=order_id)
        target.unlink(missing_ok=True)
        document = Document()
        section = document.sections[0]
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)

        styles = document.styles
        styles["Normal"].font.name = "Aptos"
        styles["Normal"].font.size = Pt(11)
        title = document.add_heading(document_value.title, level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        mode_label = _MODE_LABELS[document_value.language][document_value.mode]
        metadata = document.add_paragraph(mode_label)
        metadata.alignment = WD_ALIGN_PARAGRAPH.CENTER
        metadata.runs[0].italic = True

        segments = document_value.segments
        index = 0
        while index < len(segments):
            segment = segments[index]
            if segment.kind == "table_row":
                rows: list[list[str]] = []
                while index < len(segments) and segments[index].kind == "table_row":
                    rows.append([cell.strip() for cell in segments[index].text.split("|")])
                    index += 1
                column_count = len(rows[0])
                if column_count < 2 or any(len(row) != column_count for row in rows):
                    raise AIContractError("rewrite table rows have inconsistent cell counts")
                table = document.add_table(rows=len(rows), cols=column_count)
                table.style = "Table Grid"
                for row_index, values in enumerate(rows):
                    for column_index, text in enumerate(values):
                        table.cell(row_index, column_index).text = text
                continue
            if segment.kind == "list_item":
                numbered = bool(re.match(r"^\s*\d+[.)]\s+", segment.text))
                list_text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", segment.text)
                paragraph = document.add_paragraph(
                    list_text, style="List Number" if numbered else "List Bullet"
                )
            else:
                paragraph = document.add_paragraph(segment.text)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.space_after = Pt(8)
            index += 1

        document.core_properties.title = document_value.title
        document.core_properties.subject = f"REWRITE:{document_value.mode}"
        document.save(str(target))
        validate_docx_security(target)
        return target
