"""Professional editable DOCX rendering for generic WRITE_TEXT documents."""

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from modules.ai.write_text_contracts import WriteTextDocument, assert_write_text_artifact_safe
from modules.service_orders.documents import DocumentTooLarge, validate_docx_security

_LABELS = {
    "uz": {"kind": "MATN", "type": "Matn turi", "style": "Uslub"},
    "ru": {"kind": "ТЕКСТ", "type": "Тип текста", "style": "Стиль"},
    "en": {"kind": "TEXT", "type": "Text type", "style": "Style"},
}

_TYPE_LABELS = {
    "uz": {"article": "Maqola", "post": "Post", "description": "Tavsif", "letter": "Xat"},
    "ru": {
        "article": "Статья",
        "post": "Публикация",
        "description": "Описание",
        "letter": "Письмо",
    },
    "en": {"article": "Article", "post": "Post", "description": "Description", "letter": "Letter"},
}

_STYLE_LABELS = {
    "uz": {"simple": "Sodda", "formal": "Rasmiy", "academic": "Akademik", "creative": "Ijodiy"},
    "ru": {
        "simple": "Простой",
        "formal": "Официальный",
        "academic": "Академический",
        "creative": "Творческий",
    },
    "en": {"simple": "Simple", "formal": "Formal", "academic": "Academic", "creative": "Creative"},
}


class WriteTextDocumentRenderer:
    def __init__(
        self, *, font_name: str = "Noto Serif", max_output_bytes: int = 20_000_000
    ) -> None:
        if not font_name.strip() or len(font_name) > 100 or max_output_bytes < 1:
            raise ValueError("invalid write text document limits")
        self._font_name = font_name.strip()
        self._max_output_bytes = max_output_bytes

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"matn_{order_id}.docx"

    def render(self, content: WriteTextDocument, output_dir: Path, *, order_id: int) -> Path:
        assert_write_text_artifact_safe(content)
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure(document)
        labels = _LABELS[content.language]

        kind = document.add_paragraph(labels["kind"])
        kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
        kind.runs[0].bold = True
        kind.runs[0].font.size = Pt(12)

        title = document.add_paragraph(content.title)
        title.style = document.styles["Title"]
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER

        metadata = document.add_paragraph(
            f"{labels['type']}: {_TYPE_LABELS[content.language][content.text_type]}  •  "
            f"{labels['style']}: {_STYLE_LABELS[content.language][content.style]}"
        )
        metadata.alignment = WD_ALIGN_PARAGRAPH.CENTER
        metadata.runs[0].italic = True

        for block in content.blocks:
            if block.heading:
                heading = document.add_heading(block.heading, level=1)
                heading.paragraph_format.keep_with_next = True
            for text in block.paragraphs:
                paragraph = document.add_paragraph(text)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                paragraph.paragraph_format.first_line_indent = Cm(1.25)
                paragraph.paragraph_format.line_spacing = 1.5
                paragraph.paragraph_format.space_after = Pt(8)

        document.save(str(output))
        if not output.is_file() or output.stat().st_size > self._max_output_bytes:
            output.unlink(missing_ok=True)
            raise DocumentTooLarge("WRITE_TEXT DOCX exceeded artifact limit")
        validate_docx_security(output)
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
        for style_name in ("Title", "Heading 1", "Heading 2"):
            style = document.styles[style_name]
            style.font.name = self._font_name
            style._element.rPr.rFonts.set(qn("w:eastAsia"), self._font_name)
