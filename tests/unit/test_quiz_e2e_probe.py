from pathlib import Path

import pytest

from modules.ai.providers.mock import MockAIProvider
from modules.ai.quiz_contracts import QuizGenerationRequest
from modules.service_orders.quiz_documents import QuizDocumentRenderer
from scripts.local_quiz_e2e import (
    _assert_structure_parity,
    _parse_quiz_structure,
    _validate_docx,
)


@pytest.mark.asyncio
async def test_real_quiz_e2e_docx_probe_validates_answer_key_and_evidence(tmp_path: Path) -> None:
    request = QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=4,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="uz",
        additional_requirements="Amaliy tekshiruv iborasini talab dalilida aynan kiriting.",
    )
    quiz = await MockAIProvider().generate_quiz(request)
    path = QuizDocumentRenderer().render(quiz, tmp_path, order_id=77)

    result = _validate_docx(
        path,
        language="uz",
        topic=request.topic,
        question_count=request.question_count,
        option_count=request.option_count,
        required_phrase="amaliy tekshiruv",
    )

    assert result["question_count"] == 4
    assert result["answer_key_integrity"] is True
    assert result["topic_present"] is True
    assert result["requirement_present"] is True
    assert len(str(result["sha256"])) == 64


def test_quiz_e2e_docx_probe_rejects_non_docx(tmp_path: Path) -> None:
    invalid = tmp_path / "quiz.docx"
    invalid.write_text("not a document", encoding="utf-8")
    with pytest.raises(RuntimeError, match="magic"):
        _validate_docx(
            invalid,
            language="uz",
            topic="Algoritmik fikrlash",
            question_count=4,
            option_count=4,
            required_phrase="amaliy tekshiruv",
        )


def test_quiz_e2e_parser_enforces_exact_options_and_cross_format_parity() -> None:
    lines = [
        "1. Amaliy vaziyatda qaysi",
        "yondashuv to‘g‘ri?",
        "A) Tahlil qilish",
        "B) Taxmin",
        "qilish",
        "C) E’tiborsiz qoldirish",
        "ANSWER KEY",
        "1. A) Tahlil",
        "qilish",
        "Short explanations",
        "1. Bu satr answerga qo‘shilmasligi kerak.",
    ]
    structure = _parse_quiz_structure(
        lines,
        answer_heading="ANSWER KEY",
        question_count=1,
        option_count=3,
        source="test",
    )
    _assert_structure_parity(structure, structure)

    with pytest.raises(RuntimeError, match="exact option count"):
        _parse_quiz_structure(
            lines,
            answer_heading="ANSWER KEY",
            question_count=1,
            option_count=4,
            source="test",
        )
    drifted = {1: {**structure[1], "options": ["Buzilgan", *structure[1]["options"][1:]]}}
    with pytest.raises(RuntimeError, match="option parity"):
        _assert_structure_parity(structure, drifted)


def test_quiz_e2e_parser_accepts_detached_option_and_answer_labels() -> None:
    lines = [
        "1. Which practical method is correct?",
        "A)",
        "Correct method",
        "B)",
        "Wrong method",
        "ANSWER KEY",
        "1. A)",
        "Correct method",
        "Short explanations",
    ]
    structure = _parse_quiz_structure(
        lines,
        answer_heading="ANSWER KEY",
        question_count=1,
        option_count=2,
        source="wrapped",
    )
    assert structure[1]["options"] == ["Correct method", "Wrong method"]
    assert structure[1]["answer"] == "Correct method"


def test_quiz_e2e_parser_supports_answer_key_disabled() -> None:
    lines = [
        "1. Which practical method is correct?",
        "A) Correct method",
        "B) Wrong method",
    ]
    structure = _parse_quiz_structure(
        lines,
        answer_heading=None,
        question_count=1,
        option_count=2,
        source="hidden-key",
    )
    assert "answer" not in structure[1]
    _assert_structure_parity(structure, structure)


@pytest.mark.asyncio
async def test_quiz_e2e_parser_matches_real_english_renderer_answer_heading(
    tmp_path: Path,
) -> None:
    request = QuizGenerationRequest(
        topic="Project management",
        question_count=2,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="en",
        additional_requirements=None,
    )
    quiz = await MockAIProvider().generate_quiz(request)
    path = QuizDocumentRenderer().render(quiz, tmp_path, order_id=1)

    result = _validate_docx(
        path,
        topic=request.topic,
        question_count=request.question_count,
        option_count=request.option_count,
        language=request.language,
        required_phrase="practical situation",
        show_answer_key=True,
    )

    assert result["answer_key_integrity"] is True


def test_quiz_e2e_parser_rejects_detached_empty_content_and_wrong_answer_label() -> None:
    with pytest.raises(RuntimeError, match="empty detached option"):
        _parse_quiz_structure(
            ["1. Question?", "A)", "B) Other", "ANSWER KEY", "1. B) Other"],
            answer_heading="ANSWER KEY",
            question_count=1,
            option_count=2,
            source="empty",
        )

    with pytest.raises(RuntimeError, match="answer-key label mismatch"):
        _parse_quiz_structure(
            [
                "1. Question?",
                "A) Correct",
                "B) Wrong",
                "ANSWER KEY",
                "1. B) Correct",
            ],
            answer_heading="ANSWER KEY",
            question_count=1,
            option_count=2,
            source="wrong-label",
        )
