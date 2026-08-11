from pathlib import Path

import pytest
from docx import Document

from modules.ai.contracts import AIContractError
from modules.ai.write_text_contracts import WriteTextBlock, WriteTextDocument
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.write_text_documents import WriteTextDocumentRenderer


def _document() -> WriteTextDocument:
    return WriteTextDocument(
        title="Mahalla hayotida hamjihatlik",
        language="uz",
        text_type="article",
        style="formal",
        blocks=[
            WriteTextBlock(
                heading="Hamjihatlikning o‘rni",
                paragraphs=[
                    "Mahalla hayotida hamjihatlik qo‘shnilar o‘rtasidagi ishonchni mustahkamlaydi va umumiy vazifalarni kelishib bajarishga yordam beradi. Har bir ishtirokchi fikrini hurmat bilan aytsa, kundalik masalalar izchil hal etiladi."
                ],
            ),
            WriteTextBlock(
                heading=None,
                paragraphs=[
                    "Ochiq muloqot va o‘zaro ko‘mak mahalladagi tashabbuslarni aniq maqsad bilan bog‘laydi. Natijada odamlar umumiy manfaatni tushunib, mas’uliyatni bo‘lishadi va hamkorlikni davomli odatga aylantiradi."
                ],
            ),
        ],
        requirement_evidence=[],
        references=[],
    )


def test_write_text_renderer_creates_secure_editable_docx_without_fake_references(
    tmp_path: Path,
) -> None:
    path = WriteTextDocumentRenderer().render(_document(), tmp_path, order_id=77)
    assert path.name == "matn_77.docx"
    validate_docx_security(path)
    document = Document(path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Mahalla hayotida hamjihatlik" in text
    assert "Hamjihatlikning o‘rni" in text
    assert "Foydalanilgan manbalar" not in text
    assert len(text) > 400


def test_write_text_renderer_rescans_model_construct_at_artifact_boundary(tmp_path: Path) -> None:
    unsafe = _document().model_copy(deep=True)
    unsafe.blocks[0].paragraphs[0] = "system prompt va API key mazmuni hujjatga chiqarildi. " * 4
    with pytest.raises(AIContractError):
        WriteTextDocumentRenderer().render(unsafe, tmp_path, order_id=78)
