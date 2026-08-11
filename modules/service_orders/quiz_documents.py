"""Professional editable DOCX rendering for production quizzes."""

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.section import WD_SECTION_START
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from modules.ai.quiz_contracts import QuizDocument, assert_quiz_artifact_safe
from modules.service_orders.documents import (
    DocumentTooLarge,
    EssayDocumentRenderer,
    UnsafeDocument,
    validate_docx_security,
)

_LABELS = {
    "uz": {
        "kind": "QUIZ",
        "topic": "Mavzu",
        "difficulty": "Qiyinlik",
        "count": "Savollar soni",
        "instructions": "Ko‘rsatma",
        "questions": "SAVOLLAR",
        "answer_key": "JAVOBLAR KALITI",
        "explanations": "Qisqa izohlar",
        "requirements": "Qo‘shimcha talablarning qo‘llanishi",
        "sources": "Manbalar holati",
        "source_notice": "Tekshirilgan tashqi manbalar taqdim etilmadi; references bo‘limi ataylab bo‘sh qoldirildi.",
    },
    "ru": {
        "kind": "ТЕСТ",
        "topic": "Тема",
        "difficulty": "Сложность",
        "count": "Количество вопросов",
        "instructions": "Инструкция",
        "questions": "ВОПРОСЫ",
        "answer_key": "КЛЮЧ ОТВЕТОВ",
        "explanations": "Краткие пояснения",
        "requirements": "Применение дополнительных требований",
        "sources": "Состояние источников",
        "source_notice": "Проверенные внешние источники не предоставлены; раздел references намеренно оставлен пустым.",
    },
    "en": {
        "kind": "QUIZ",
        "topic": "Topic",
        "difficulty": "Difficulty",
        "count": "Question count",
        "instructions": "Instructions",
        "questions": "QUESTIONS",
        "answer_key": "ANSWER KEY",
        "explanations": "Brief explanations",
        "requirements": "Application of additional requirements",
        "sources": "Source status",
        "source_notice": "No verified external sources were provided; the references section is intentionally empty.",
    },
}
_DIFFICULTY = {
    "uz": {"easy": "Oson", "medium": "O‘rta", "hard": "Qiyin"},
    "ru": {"easy": "Легко", "medium": "Средне", "hard": "Сложно"},
    "en": {"easy": "Easy", "medium": "Medium", "hard": "Hard"},
}
_OPTION_LABELS = "ABCDE"


class QuizDocumentRenderer(EssayDocumentRenderer):
    """Render a secure, editable and request-bound quiz document."""

    @staticmethod
    def output_path(output_dir: Path, *, order_id: int) -> Path:
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        root = output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / f"quiz_{order_id}.docx"

    def render(  # type: ignore[override]
        self,
        quiz: QuizDocument,
        output_dir: Path,
        *,
        order_id: int,
    ) -> Path:
        assert_quiz_artifact_safe(quiz)
        output = self.output_path(output_dir, order_id=order_id)
        output.unlink(missing_ok=True)
        document = Document()
        self._configure_document(document)
        for section in document.sections:
            section.top_margin = Inches(0.7)
            section.bottom_margin = Inches(0.7)
            section.left_margin = Inches(0.8)
            section.right_margin = Inches(0.8)
        labels = _LABELS[quiz.language]
        self._add_quiz_title_page(document, quiz, labels)
        self._add_heading(document, labels["questions"], level=1)
        for question in quiz.questions:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.keep_with_next = True
            paragraph.paragraph_format.space_before = Pt(10)
            paragraph.paragraph_format.space_after = Pt(5)
            run = paragraph.add_run(f"{question.number}. {question.text}")
            run.bold = True
            run.font.size = Pt(11)
            for index, option in enumerate(question.options):
                option_paragraph = document.add_paragraph(
                    f"{_OPTION_LABELS[index]}) {option}", style="List Bullet"
                )
                option_paragraph.paragraph_format.left_indent = Inches(0.3)
                option_paragraph.paragraph_format.space_after = Pt(2)

        if quiz.show_answer_key:
            document.add_section(WD_SECTION_START.NEW_PAGE)
            self._add_heading(document, labels["answer_key"], level=1)
            for answer in quiz.answer_key:
                question = quiz.questions[answer.question_number - 1]
                option_index = question.options.index(answer.correct_answer)
                document.add_paragraph(
                    f"{answer.question_number}. {_OPTION_LABELS[option_index]}) {answer.correct_answer}"
                )
            explanations = [item for item in quiz.questions if item.explanation]
            if explanations:
                self._add_heading(document, labels["explanations"], level=2)
                for question in explanations:
                    self._add_body_paragraph(document, f"{question.number}. {question.explanation}")

        if quiz.requirement_evidence:
            self._add_heading(document, labels["requirements"], level=1)
            for evidence in quiz.requirement_evidence:
                self._add_body_paragraph(document, evidence)
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

    def _add_quiz_title_page(
        self,
        document: DocumentObject,
        quiz: QuizDocument,
        labels: dict[str, str],
    ) -> None:
        document.add_paragraph()
        kind = document.add_paragraph(labels["kind"])
        kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
        kind.runs[0].bold = True
        kind.runs[0].font.size = Pt(18)
        title = document.add_paragraph(quiz.title, style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_before = Pt(54)
        title.paragraph_format.space_after = Pt(22)
        metadata = (
            f"{labels['topic']}: {quiz.topic}\n"
            f"{labels['difficulty']}: {_DIFFICULTY[quiz.language][quiz.difficulty]}\n"
            f"{labels['count']}: {len(quiz.questions)}"
        )
        meta = document.add_paragraph(metadata)
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta.paragraph_format.space_after = Pt(24)
        heading = document.add_paragraph(labels["instructions"])
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading.runs[0].bold = True
        instructions = document.add_paragraph(quiz.instructions)
        instructions.alignment = WD_ALIGN_PARAGRAPH.CENTER
        generated = document.add_paragraph("Presentation AI Nodirbek")
        generated.alignment = WD_ALIGN_PARAGRAPH.CENTER
        generated.paragraph_format.space_before = Pt(42)
        generated.runs[0].italic = True
        document.add_page_break()  # type: ignore[no-untyped-call]
