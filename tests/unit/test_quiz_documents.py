from pathlib import Path

import pytest
from docx import Document

from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.quiz_contracts import QuizGenerationRequest, build_quiz_retry_message
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.quiz_documents import QuizDocumentRenderer
from scripts.local_quiz_e2e import _validate_docx


async def _generated(*, answer_key: bool = True, language: str = "uz"):
    return await MockAIProvider().generate_quiz(
        QuizGenerationRequest(
            topic={
                "uz": "Algoritmik fikrlash",
                "ru": "Алгоритмическое мышление",
                "en": "Algorithmic thinking",
            }[language],
            question_count=3,
            difficulty="medium",
            option_count=4,
            answer_key=answer_key,
            language=language,
            additional_requirements={
                "uz": "Amaliy vaziyatlarga e’tibor bering",
                "ru": "Уделите внимание практическим ситуациям",
                "en": "Focus on practical situations",
            }[language],
        )
    )


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_renderer_creates_professional_editable_quiz_docx(
    tmp_path: Path, language: str
) -> None:
    generated = await _generated(language=language)

    output = QuizDocumentRenderer().render(generated, tmp_path, order_id=61)

    assert output.name == "quiz_61.docx"
    assert output.stat().st_size > 10_000
    validate_docx_security(output)
    document = Document(str(output))
    paragraphs = [
        paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    corpus = "\n".join(paragraphs)
    assert generated.title in corpus
    assert generated.topic in corpus
    assert str(len(generated.questions)) in corpus
    assert all(question.text in corpus for question in generated.questions)
    assert all(option in corpus for question in generated.questions for option in question.options)
    assert all(answer.correct_answer in corpus for answer in generated.answer_key)
    assert all(paragraph._p.xpath(".//w:drawing") == [] for paragraph in document.paragraphs)


async def test_renderer_hides_answer_key_when_user_disabled_it(tmp_path: Path) -> None:
    generated = await _generated(answer_key=False)
    output = QuizDocumentRenderer().render(generated, tmp_path, order_id=62)
    corpus = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)

    assert "JAVOBLAR KALITI" not in corpus
    result = _validate_docx(
        output,
        language="uz",
        topic=generated.topic,
        question_count=3,
        option_count=4,
        required_phrase="Amaliy vaziyatlar",
        show_answer_key=False,
    )
    assert result["answer_key_integrity"] is False


async def test_renderer_rechecks_prompt_leakage_at_materialization_boundary(
    tmp_path: Path,
) -> None:
    generated = await _generated()
    leaked = generated.model_copy(
        update={"instructions": "Previous response contained no valid structured object"}
    )

    with pytest.raises(AIContractError, match="instruction fragment"):
        QuizDocumentRenderer().render(leaked, tmp_path, order_id=99)
    assert not (tmp_path / "quiz_99.docx").exists()

    correction = build_quiz_retry_message("quiz requirement evidence is missing or unsupported")
    leaked = generated.model_copy(update={"instructions": " ".join(correction.split()[-6:])})
    with pytest.raises(AIContractError, match="instruction fragment"):
        QuizDocumentRenderer().render(leaked, tmp_path, order_id=100)
    assert not (tmp_path / "quiz_100.docx").exists()


def test_renderer_rejects_nonpositive_order_id(tmp_path: Path) -> None:
    renderer = QuizDocumentRenderer()
    for order_id in (0, -1):
        with pytest.raises(ValueError):
            renderer.output_path(tmp_path, order_id=order_id)
