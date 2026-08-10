from pathlib import Path

from docx import Document

from modules.ai.providers.mock import MockAIProvider
from modules.ai.summary_contracts import SummaryGenerationRequest
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.summary_documents import SummaryDocumentRenderer


async def test_renderer_creates_professional_editable_summary_docx(tmp_path: Path) -> None:
    generated = await MockAIProvider().generate_summary(
        SummaryGenerationRequest(
            source=(
                "Algoritmik fikrlash vazifani qismlarga ajratadi. Inson nazorati "
                "yechimni tekshiradi va amaliy xavfni kamaytiradi."
            ),
            detail="short",
            language="uz",
            additional_requirements="Inson nazorati rolini alohida tushuntiring.",
        )
    )

    output = SummaryDocumentRenderer().render(generated, tmp_path, order_id=51)

    assert output.name == "konspekt_51.docx"
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
    assert "KONSPEKT" in paragraphs
    assert generated.title in paragraphs
    assert "Qisqacha mazmun" in headings
    assert "Asosiy fikrlar" in headings
    assert "Asosiy tushunchalar" in headings
    assert "Amaliy jarayon" in headings
    assert "Xulosa" in headings
    assert "Manbalar holati" in headings
    assert "inson nazorati" in "\n".join(paragraphs).casefold()
    assert any("Tekshirilgan tashqi manbalar" in text for text in paragraphs)
    assert all("system prompt" not in text.casefold() for text in paragraphs)
    assert output.stat().st_size > 10_000


def test_summary_renderer_rejects_nonpositive_order_id(tmp_path: Path) -> None:
    renderer = SummaryDocumentRenderer()
    for order_id in (0, -1):
        try:
            renderer.output_path(tmp_path, order_id=order_id)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe order id was accepted")
