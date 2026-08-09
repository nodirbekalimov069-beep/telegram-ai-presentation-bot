"""Professional DOCX rendering for the production Mustaqil ish service."""

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from modules.ai.independent_work_contracts import IndependentWorkDocument
from modules.service_orders.documents import (
    DocumentTooLarge,
    EssayDocumentRenderer,
    UnsafeDocument,
    validate_docx_security,
)

_LABELS = {
    "uz": {
        "kind": "MUSTAQIL ISH",
        "subject": "Fan",
        "contents": "Mundarija",
        "objective": "Maqsad",
        "tasks": "Amaliy vazifalar",
        "introduction": "Kirish",
        "conclusion": "Xulosa",
        "references": "Foydalanilgan adabiyotlar",
        "reference_notice": "Tekshirilgan bibliografik manbalar taqdim etilmadi.",
        "toc_placeholder": "Word/LibreOffice: mundarijani yangilang",
    },
    "ru": {
        "kind": "САМОСТОЯТЕЛЬНАЯ РАБОТА",
        "subject": "Предмет",
        "contents": "Содержание",
        "objective": "Цель",
        "tasks": "Практические задачи",
        "introduction": "Введение",
        "conclusion": "Заключение",
        "references": "Список литературы",
        "reference_notice": "Проверенные библиографические источники не предоставлены.",
        "toc_placeholder": "Word/LibreOffice: обновите содержание",
    },
    "en": {
        "kind": "INDEPENDENT ASSIGNMENT",
        "subject": "Subject",
        "contents": "Contents",
        "objective": "Objective",
        "tasks": "Practical tasks",
        "introduction": "Introduction",
        "conclusion": "Conclusion",
        "references": "References",
        "reference_notice": "No verified bibliographic sources were supplied.",
        "toc_placeholder": "Word/LibreOffice: update the table of contents",
    },
}


class IndependentWorkDocumentRenderer(EssayDocumentRenderer):
    """Reuse the audited typography/OOXML primitives without duplicating essay workflow code."""

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"mustaqil_ish_{order_id}.docx"

    def render(  # type: ignore[override]
        self,
        essay: IndependentWorkDocument,
        output_dir: Path,
        *,
        order_id: int,
    ) -> Path:
        work = essay
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure_document(document)
        labels = _LABELS[work.language]
        self._add_independent_title_page(document, work, labels)
        self._add_contents(document, labels)
        self._add_heading(document, labels["objective"], level=1)
        self._add_body_paragraph(document, work.objective)
        self._add_heading(document, labels["tasks"], level=1)
        for task in work.practical_tasks:
            paragraph = document.add_paragraph(task, style="List Bullet")
            paragraph.paragraph_format.space_after = Pt(4)
        self._add_heading(document, labels["introduction"], level=1)
        self._add_body_paragraph(document, work.introduction)
        for section in work.sections:
            self._add_heading(document, section.heading, level=1)
            for body_text in section.paragraphs:
                self._add_body_paragraph(document, body_text)
        self._add_heading(document, labels["conclusion"], level=1)
        self._add_body_paragraph(document, work.conclusion)
        self._add_heading(document, labels["references"], level=1)
        notice = document.add_paragraph(labels["reference_notice"])
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

    def _add_independent_title_page(
        self,
        document: DocumentObject,
        work: IndependentWorkDocument,
        labels: dict[str, str],
    ) -> None:
        document.add_paragraph()
        kind = document.add_paragraph(labels["kind"])
        kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
        kind.runs[0].bold = True
        kind.runs[0].font.size = Pt(18)
        subject = document.add_paragraph(f"{labels['subject']}: {work.subject}")
        subject.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subject.paragraph_format.space_before = Pt(18)
        title = document.add_paragraph(work.title, style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_before = Pt(54)
        title.paragraph_format.space_after = Pt(24)
        generated = document.add_paragraph("Presentation AI Nodirbek")
        generated.alignment = WD_ALIGN_PARAGRAPH.CENTER
        generated.runs[0].italic = True
        document.add_page_break()  # type: ignore[no-untyped-call]
