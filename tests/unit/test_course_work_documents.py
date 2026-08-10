from pathlib import Path

from docx import Document

from modules.ai.course_work_contracts import CourseWorkGenerationRequest
from modules.ai.providers.mock import MockAIProvider
from modules.service_orders.course_work_documents import CourseWorkDocumentRenderer
from modules.service_orders.documents import validate_docx_security


async def test_renderer_creates_professional_editable_course_work_docx(tmp_path: Path) -> None:
    generated = await MockAIProvider().generate_course_work(
        CourseWorkGenerationRequest(
            subject="Pedagogika",
            topic="Hamkorlikdagi o‘rganish",
            sections="Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
            language="uz",
            length="short",
            additional_requirements="Xulosada xavfsiz hamkorlik mezonlarini tushuntir.",
        )
    )

    output = CourseWorkDocumentRenderer().render(generated, tmp_path, order_id=41)

    assert output.name == "kurs_ishi_41.docx"
    validate_docx_security(output)
    document = Document(str(output))
    paragraphs = [
        paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    headings = {
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.style is not None
        and paragraph.style.name.startswith("Heading")
        and paragraph.text.strip()
    }
    assert "KURS ISHI" in paragraphs
    assert "Fan: Pedagogika" in paragraphs
    assert "Maqsad" in headings
    assert "Vazifalar" in headings
    assert "Kirish" in headings
    assert "Nazariy asoslar" in headings
    assert "Amaliy tahlil" in headings
    assert "Tavsiyalar" in headings
    assert "Xulosa" in headings
    assert "Foydalanilgan adabiyotlar" in headings
    assert "xavfsiz hamkorlik mezonlarini" in "\n".join(paragraphs)
    assert any("Tekshirilgan bibliografik manbalar" in text for text in paragraphs)
    assert output.stat().st_size > 10_000


def test_course_renderer_rejects_nonpositive_order_id(tmp_path: Path) -> None:
    renderer = CourseWorkDocumentRenderer()
    for order_id in (0, -1):
        try:
            renderer.output_path(tmp_path, order_id=order_id)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe order id was accepted")
