"""Professional editable DOCX rendering for production summary notes."""

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from modules.ai.summary_contracts import SummaryDocument
from modules.service_orders.documents import (
    DocumentTooLarge,
    EssayDocumentRenderer,
    UnsafeDocument,
    validate_docx_security,
)

_LABELS = {
    "uz": {
        "kind": "KONSPEKT",
        "overview": "Qisqacha mazmun",
        "key_points": "Asosiy fikrlar",
        "conclusion": "Xulosa",
        "sources": "Manbalar holati",
        "source_notice": "Tekshirilgan tashqi manbalar taqdim etilmadi; mazmun foydalanuvchi matniga bog‘langan.",
    },
    "ru": {
        "kind": "КОНСПЕКТ",
        "overview": "Краткое содержание",
        "key_points": "Основные положения",
        "conclusion": "Вывод",
        "sources": "Состояние источников",
        "source_notice": "Проверенные внешние источники не предоставлены; содержание связано с текстом пользователя.",
    },
    "en": {
        "kind": "SUMMARY NOTES",
        "overview": "Overview",
        "key_points": "Key points",
        "conclusion": "Conclusion",
        "sources": "Source status",
        "source_notice": "No verified external sources were supplied; the content is bound to the user text.",
    },
}


class SummaryDocumentRenderer(EssayDocumentRenderer):
    """Render service-specific study notes using audited OOXML primitives."""

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"konspekt_{order_id}.docx"

    def render(  # type: ignore[override]
        self,
        summary: SummaryDocument,
        output_dir: Path,
        *,
        order_id: int,
    ) -> Path:
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure_document(document)
        labels = _LABELS[summary.language]
        self._add_summary_title_page(document, summary, labels)
        self._add_heading(document, labels["overview"], level=1)
        self._add_body_paragraph(document, summary.overview)
        self._add_heading(document, labels["key_points"], level=1)
        for point in summary.key_points:
            paragraph = document.add_paragraph(point, style="List Bullet")
            paragraph.paragraph_format.space_after = Pt(4)
        for section in summary.sections:
            self._add_heading(document, section.heading, level=1)
            for body_text in section.paragraphs:
                self._add_body_paragraph(document, body_text)
        self._add_heading(document, labels["conclusion"], level=1)
        self._add_body_paragraph(document, summary.conclusion)
        self._add_heading(document, labels["sources"], level=1)
        notice = document.add_paragraph(labels["source_notice"])
        notice.runs[0].italic = True
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

    def _add_summary_title_page(
        self,
        document: DocumentObject,
        summary: SummaryDocument,
        labels: dict[str, str],
    ) -> None:
        document.add_paragraph()
        kind = document.add_paragraph(labels["kind"])
        kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
        kind.runs[0].bold = True
        kind.runs[0].font.size = Pt(18)
        title = document.add_paragraph(summary.title, style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_before = Pt(72)
        title.paragraph_format.space_after = Pt(24)
        generated = document.add_paragraph("Presentation AI Nodirbek")
        generated.alignment = WD_ALIGN_PARAGRAPH.CENTER
        generated.runs[0].italic = True
        document.add_page_break()  # type: ignore[no-untyped-call]
