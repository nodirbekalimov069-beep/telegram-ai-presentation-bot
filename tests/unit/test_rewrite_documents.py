from pathlib import Path

from docx import Document

from modules.ai.providers.mock import MockAIProvider
from modules.ai.rewrite_contracts import (
    RewriteDocument,
    RewriteGenerationRequest,
    RewriteSegment,
)
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.rewrite_documents import RewriteDocumentRenderer


async def test_rewrite_renderer_creates_secure_editable_structured_docx(tmp_path: Path) -> None:
    generated = await MockAIProvider().generate_rewrite(
        RewriteGenerationRequest(
            text=(
                "Jamoa vazifalarni umumiy reja asosida muvofiqlashtiradi va har bir natijani "
                "yetkazishdan oldin sinchiklab tekshiradi."
            ),
            mode="formal",
        )
    )

    path = RewriteDocumentRenderer().render(generated, tmp_path, order_id=77)

    assert path.name == "qayta_yozish_77.docx"
    validate_docx_security(path)
    document = Document(path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert generated.title in text
    assert generated.segments[0].text in text
    assert "Rasmiy" in text
    assert "Foydalanilgan manbalar" not in text
    assert len(text) > 150


def test_rewrite_renderer_materializes_lists_and_editable_tables(tmp_path: Path) -> None:
    value = RewriteDocument(
        title="Rewritten text",
        language="en",
        mode="formal",
        segments=[
            RewriteSegment(
                source_index=0,
                kind="list_item",
                text="- Furthermore, the first verified task is assigned to the owner.",
            ),
            RewriteSegment(
                source_index=1,
                kind="list_item",
                text="1. Furthermore, the numbered task is assigned to the reviewer.",
            ),
            RewriteSegment(
                source_index=2,
                kind="table_row",
                text="item | amount | status",
            ),
            RewriteSegment(
                source_index=3,
                kind="table_row",
                text="Nodir | 12 | formally ready",
            ),
        ],
        references=[],
    )
    path = RewriteDocumentRenderer().render(value, tmp_path, order_id=78)
    document = Document(path)
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.rows) == 2
    assert len(table.columns) == 3
    assert [cell.text for cell in table.rows[0].cells] == ["item", "amount", "status"]
    assert [cell.text for cell in table.rows[1].cells] == [
        "Nodir",
        "12",
        "formally ready",
    ]
    list_paragraph = next(
        paragraph for paragraph in document.paragraphs if "first verified" in paragraph.text
    )
    assert not list_paragraph.text.startswith("-")
    assert list_paragraph.style.name == "List Bullet"
    numbered_paragraph = next(
        paragraph for paragraph in document.paragraphs if "numbered task" in paragraph.text
    )
    assert not numbered_paragraph.text.startswith("1.")
    assert numbered_paragraph.style.name == "List Number"
