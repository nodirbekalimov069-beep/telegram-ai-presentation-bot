from pathlib import Path

from docx import Document

from modules.ai.independent_work_contracts import IndependentWorkGenerationRequest
from modules.ai.providers.mock import MockAIProvider
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.independent_work_documents import IndependentWorkDocumentRenderer


async def test_renderer_creates_professional_independent_work_docx(tmp_path: Path) -> None:
    generated = await MockAIProvider().generate_independent_work(
        IndependentWorkGenerationRequest(
            subject="Pedagogika",
            topic="Interfaol ta’lim metodlari",
            language="uz",
            length="short",
            additional_requirements="Xulosada hamkorlikdagi o‘rganish iborasini aynan ishlat.",
        )
    )

    output = IndependentWorkDocumentRenderer().render(generated, tmp_path, order_id=17)

    assert output.name == "mustaqil_ish_17.docx"
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
    assert "MUSTAQIL ISH" in paragraphs
    assert "Fan: Pedagogika" in paragraphs
    assert "Maqsad" in headings
    assert "Amaliy vazifalar" in headings
    assert "Kirish" in headings
    assert "Xulosa" in headings
    assert "Foydalanilgan adabiyotlar" in headings
    assert "hamkorlikdagi o‘rganish" in "\n".join(paragraphs)
    assert any("Tekshirilgan bibliografik manbalar" in text for text in paragraphs)
    assert output.stat().st_size > 10_000


def test_renderer_rejects_unsafe_order_id(tmp_path: Path) -> None:
    renderer = IndependentWorkDocumentRenderer()

    for order_id in (0, -1):
        try:
            renderer.output_path(tmp_path, order_id=order_id)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe order id was accepted")
