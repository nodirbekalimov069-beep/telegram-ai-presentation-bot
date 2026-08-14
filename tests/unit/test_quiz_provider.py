from typing import Literal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError
from pydantic import ValidationError

from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.quiz_contracts import (
    QuizAnswer,
    QuizDocument,
    QuizGenerationRequest,
    QuizQuestion,
    build_quiz_messages,
)


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_mock_provider_generates_request_bound_quiz(
    language: Literal["uz", "ru", "en"],
) -> None:
    request = QuizGenerationRequest(
        topic={
            "uz": "Algoritmik fikrlash",
            "ru": "Алгоритмическое мышление",
            "en": "Algorithmic thinking",
        }[language],
        question_count=5,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language=language,
        additional_requirements={
            "uz": "Amaliy vaziyatlarga e’tibor bering",
            "ru": "Уделите внимание практическим ситуациям",
            "en": "Focus on practical situations",
        }[language],
    )

    result = await MockAIProvider().generate_quiz(request)

    assert result.topic == request.topic
    assert result.language == language
    assert result.difficulty == "medium"
    assert len(result.questions) == 5
    assert all(len(question.options) == 4 for question in result.questions)
    assert len({question.text.casefold() for question in result.questions}) == 5
    assert len(result.answer_key) == 5
    assert result.requirement_evidence
    assert result.references == []


async def test_openai_provider_uses_strict_quiz_schema_and_validator() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    request = QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=1,
        difficulty="easy",
        option_count=2,
        answer_key=True,
        language="uz",
    )
    question = QuizQuestion(
        number=1,
        text="Algoritmik fikrlashda vazifani boshqarishning to‘g‘ri yo‘li qaysi?",
        options=["Vazifani bosqichlarga ajratish", "Natijani tekshirmaslik"],
        correct_answer="Vazifani bosqichlarga ajratish",
        explanation="Vazifani bosqichlarga ajratish jarayonni nazorat qilish imkonini beradi.",
        citation_indices=[],
    )
    document = QuizDocument(
        title="Algoritmik fikrlash quiz",
        topic=request.topic,
        language="uz",
        difficulty="easy",
        show_answer_key=True,
        instructions="Har bir savolda faqat bitta to‘g‘ri javobni tanlang.",
        questions=[question],
        answer_key=[QuizAnswer(question_number=1, correct_answer=question.correct_answer)],
        requirement_evidence=[],
        references=[],
    )
    provider._request_structured = AsyncMock(return_value=document)  # type: ignore[method-assign]

    result = await provider.generate_quiz(request)

    assert result is document
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is QuizDocument
    assert call["response_validator"](document) is document
    assert request.topic in list(call["messages"])[1]["content"]
    with pytest.raises(AIContractError, match="question count"):
        call["response_validator"](document.model_copy(update={"questions": []}))
    await provider.aclose()


def test_quiz_initial_instruction_states_schema_and_semantic_invariants_explicitly() -> None:
    request = QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=2,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="uz",
    )

    system = " ".join(build_quiz_messages(request)[0]["content"].casefold().split())

    assert "correct_answer must exactly equal one option" in system
    assert "options must remain unique after unicode normalization" in system
    assert "exactly one defensible correct answer" in system
    assert "each explanation must support the keyed correct_answer" in system
    assert "return exactly one json object" in system


async def test_quiz_schema_retry_emits_only_finite_static_correction() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=1,
        retry_base_delay_seconds=0,
    )
    with pytest.raises(ValidationError) as captured:
        QuizQuestion(
            number=1,
            text="Algoritmik fikrlash uchun qaysi usul to‘g‘ri?",
            options=["Bir xil variant", "Bir xil variant"],
            correct_answer="Bir xil variant",
            explanation="Bir xil variant jarayonni boshqarish uchun tanlanadi.",
            citation_indices=[],
        )
    document = await MockAIProvider().generate_quiz(
        QuizGenerationRequest(
            topic="Algoritmik fikrlash",
            question_count=1,
            difficulty="easy",
            option_count=2,
            answer_key=True,
            language="uz",
        )
    )
    completion = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=document))],
    )
    parse = AsyncMock(side_effect=[captured.value, completion])
    provider._client.chat.completions.parse = parse

    result = await provider._request_structured(
        messages=[{"role": "system", "content": "Generate a quiz."}],
        response_format=QuizDocument,
        response_validator=None,
    )

    emitted = parse.await_args_list[1].kwargs["messages"][-1]["content"]
    assert result is document
    assert "duplicate options" in emitted.casefold()
    assert "one complete json object" in emitted.casefold()
    assert "quiz options must be unique" not in emitted
    await provider.aclose()


async def test_quiz_timeout_then_duplicate_feedback_produces_unique_questions_within_four_calls() -> (
    None
):
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=3,
        retry_base_delay_seconds=0,
    )
    request = QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=2,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="uz",
    )
    unique = await MockAIProvider().generate_quiz(request)
    duplicate_payload = unique.model_dump()
    duplicate_payload["questions"][1]["text"] = duplicate_payload["questions"][0]["text"]
    with pytest.raises(ValidationError, match="quiz questions must be unique") as duplicate:
        QuizDocument.model_validate(duplicate_payload)
    completion = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=unique))],
    )
    parse = AsyncMock(
        side_effect=[
            APITimeoutError(request=httpx.Request("POST", "https://example.test/v1")),
            duplicate.value,
            completion,
        ]
    )
    provider._client.chat.completions.parse = parse

    result = await provider.generate_quiz(request)

    correction = parse.await_args_list[2].kwargs["messages"][-1]["content"]
    assert parse.await_count == 3
    assert parse.await_count <= 4
    assert "repeated a question" in correction
    assert "Make every question unique" in correction
    assert "options must be unique" not in correction.casefold()
    assert "quiz questions must be unique" not in correction
    assert (
        len({question.text.casefold() for question in result.questions}) == request.question_count
    )
    await provider.aclose()
