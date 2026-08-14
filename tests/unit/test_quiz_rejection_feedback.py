import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from modules.ai.contracts import AIContractError
from modules.ai.providers.errors import QuizRejectionCategory, QuizSemanticReason

from modules.ai.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _classify_quiz_rejection,
    _quiz_corrective_feedback,
)
from modules.ai.providers.mock import MockAIProvider
from modules.ai import quiz_contracts
from modules.ai.quiz_contracts import QuizDocument, QuizGenerationRequest


def _request() -> QuizGenerationRequest:
    return QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=4,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="uz",
        additional_requirements="Amaliy vaziyatlarga e’tibor bering",
    )


async def _valid_quiz() -> QuizDocument:
    return await MockAIProvider().generate_quiz(_request())


def _duplicate_error() -> ValidationError:
    payload = asyncio.run(_valid_quiz()).model_dump()
    payload["questions"][1]["text"] = payload["questions"][0]["text"]
    with pytest.raises(ValidationError) as captured:
        QuizDocument.model_validate(payload)
    return captured.value


def _normalized_duplicate_error() -> ValidationError:
    payload = asyncio.run(_valid_quiz()).model_dump()
    original = payload["questions"][0]["text"]
    payload["questions"][1]["text"] = f"  {original.replace(' ', '   ')}  "
    with pytest.raises(ValidationError) as captured:
        QuizDocument.model_validate(payload)
    return captured.value


def test_quiz_rejection_categories_are_closed_and_provider_neutral() -> None:
    assert {category.value for category in QuizRejectionCategory} == {
        "schema_invalid",
        "duplicate",
        "semantic_invalid",
        "other_invalid",
    }


def test_quiz_semantic_reason_codes_are_closed_and_provider_neutral() -> None:
    assert {reason.value for reason in QuizSemanticReason} == {
        "invalid_correct_answer",
        "ambiguous_correct_answer",
        "invalid_options",
        "duplicate_options",
        "duplicate_question",
        "question_answer_mismatch",
        "invalid_explanation",
        "language_mismatch",
        "malformed_content",
        "other_semantic",
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ValueError("correct answer must match exactly one option"),
            "invalid_correct_answer",
        ),
        (
            AIContractError("quiz contains an ambiguous option"),
            "ambiguous_correct_answer",
        ),
        (
            AIContractError("quiz option count does not match the request"),
            "invalid_options",
        ),
        (
            ValueError("quiz options must be unique"),
            "duplicate_options",
        ),
        (
            ValueError("quiz questions must be unique"),
            "duplicate_question",
        ),
        (
            AIContractError("quiz questions ask the same fact"),
            "duplicate_question",
        ),
        (
            AIContractError("quiz question reveals its correct answer"),
            "question_answer_mismatch",
        ),
        (
            AIContractError("quiz explanation polarity contradicts the correct answer"),
            "invalid_explanation",
        ),
        (
            AIContractError("quiz authored block failed locale validation"),
            "language_mismatch",
        ),
        (
            AIContractError("quiz contains unverified numeric token"),
            "malformed_content",
        ),
        (
            AIContractError("unknown deterministic quiz invariant"),
            "other_semantic",
        ),
    ],
)
def test_sanitized_failure_shapes_map_to_bounded_semantic_reasons(
    error: Exception,
    expected: str,
) -> None:
    classifier = getattr(quiz_contracts, "classify_quiz_semantic_reason", None)
    assert classifier is not None, "bounded semantic classifier is missing"
    assert classifier(error).value == expected


@pytest.mark.parametrize("reason", list(QuizSemanticReason))
def test_semantic_reason_feedback_is_finite_specific_and_content_free(
    reason: QuizSemanticReason,
) -> None:
    raw = "TOP-SECRET-RAW-PREVIOUS-RESPONSE"
    feedback = _quiz_corrective_feedback(
        QuizRejectionCategory.SEMANTIC_INVALID,
        semantic_reason=reason,
    )

    reason_value = reason.value
    assert reason_value.replace("_", " ") in feedback.casefold()
    assert "do not repeat any previous question" in feedback.casefold()
    assert "one complete json object" in feedback.casefold()
    assert raw not in feedback
    assert len(feedback) <= 800


@pytest.mark.parametrize(
    ("error", "schema_error", "expected"),
    [
        (ValueError("malformed structured object"), True, QuizRejectionCategory.SCHEMA_INVALID),
        (_duplicate_error(), True, QuizRejectionCategory.DUPLICATE),
        (
            ValueError("request semantics were not met"),
            False,
            QuizRejectionCategory.SEMANTIC_INVALID,
        ),
        (ValueError("provider refusal"), None, QuizRejectionCategory.OTHER_INVALID),
    ],
)
def test_quiz_rejection_classification_is_bounded(
    error: Exception,
    schema_error: bool | None,
    expected: QuizRejectionCategory,
) -> None:
    assert _classify_quiz_rejection(error, schema_error=schema_error) is expected


@pytest.mark.parametrize(
    ("error", "schema_error"),
    [
        (_duplicate_error(), True),
        (_normalized_duplicate_error(), True),
        (AIContractError("quiz questions ask the same fact"), False),
        (AIContractError("quiz has duplicate question semantic core overlap"), False),
    ],
)
def test_duplicate_questions_route_to_duplicate_category_and_reason(
    error: Exception,
    schema_error: bool,
) -> None:
    duplicate_question = getattr(QuizSemanticReason, "DUPLICATE_QUESTION", None)
    assert duplicate_question is not None, "duplicate_question reason is missing"
    assert (
        _classify_quiz_rejection(error, schema_error=schema_error)
        is QuizRejectionCategory.DUPLICATE
    )
    assert quiz_contracts.classify_quiz_semantic_reason(error) is duplicate_question


def test_duplicate_question_feedback_is_finite_specific_structured_and_content_free() -> None:
    raw = "TOP-SECRET-RAW-DUPLICATE-QUESTION"
    duplicate_question = getattr(QuizSemanticReason, "DUPLICATE_QUESTION", None)
    assert duplicate_question is not None, "duplicate_question reason is missing"
    feedback = _quiz_corrective_feedback(
        QuizRejectionCategory.DUPLICATE,
        semantic_reason=duplicate_question,
    )
    normalized = feedback.casefold()

    assert "repeated a question" in normalized
    assert "every question unique" in normalized
    assert "one complete json object only" in normalized
    assert "correct every request-specific" not in normalized
    assert "option" not in normalized
    assert raw not in feedback
    assert len(feedback) <= 400


@pytest.mark.parametrize("category", list(QuizRejectionCategory))
def test_quiz_corrective_feedback_is_category_specific_bounded_and_safe(
    category: QuizRejectionCategory,
) -> None:
    secret = "sk-live-DO-NOT-COPY"
    feedback = _quiz_corrective_feedback(category)

    assert category.value.replace("_", " ") in feedback.casefold()
    assert len(feedback) <= 600
    assert secret not in feedback
    assert "previous response" in feedback.casefold()


@pytest.mark.asyncio
async def test_schema_invalid_records_category_then_corrective_feedback_recovers() -> None:
    valid = await _valid_quiz()
    schema_error = ValidationError.from_exception_data(
        "QuizDocument",
        [
            {
                "type": "missing",
                "loc": ("questions",),
                "msg": "Field required",
                "input": {"secret": "TOP-SECRET-RAW-RESPONSE"},
            }
        ],
    )
    outbound = AsyncMock(
        side_effect=[
            schema_error,
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=valid))],
            ),
        ]
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=10,
        retry_base_delay_seconds=0,
    )
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(parse=outbound)),
        close=AsyncMock(),
    )
    reserve = AsyncMock()
    record = AsyncMock()
    provider.set_quiz_call_reserver(reserve)
    provider.set_quiz_rejection_recorder(record)

    result = await provider.generate_quiz(_request())

    assert result == valid
    assert outbound.await_count == 2
    assert reserve.await_count == 2
    record.assert_awaited_once_with(QuizRejectionCategory.SCHEMA_INVALID)
    second_messages = outbound.await_args_list[1].kwargs["messages"]
    correction = second_messages[-1]["content"]
    assert "schema invalid" in correction.casefold()
    assert "TOP-SECRET-RAW-RESPONSE" not in correction


@pytest.mark.asyncio
async def test_invalid_correct_answer_schema_failure_gets_reason_specific_safe_feedback() -> None:
    valid = await _valid_quiz()
    payload = valid.model_dump()
    payload["questions"][0]["correct_answer"] = "Mavjud bo‘lmagan javob"
    with pytest.raises(ValidationError) as captured:
        QuizDocument.model_validate(payload)
    outbound = AsyncMock(
        side_effect=[
            captured.value,
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=valid))],
            ),
        ]
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=10,
        retry_base_delay_seconds=0,
    )
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(parse=outbound)),
        close=AsyncMock(),
    )
    reserve = AsyncMock()
    record = AsyncMock()
    provider.set_quiz_call_reserver(reserve)
    provider.set_quiz_rejection_recorder(record)

    result = await provider.generate_quiz(_request())

    assert result == valid
    assert outbound.await_count == 2
    assert reserve.await_count == 2
    record.assert_awaited_once_with(QuizRejectionCategory.SCHEMA_INVALID)
    correction = outbound.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "invalid correct answer" in correction.casefold()
    assert "mavjud bo‘lmagan javob" not in correction.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "first_effect", "expected_feedback"),
    [
        (
            QuizRejectionCategory.DUPLICATE,
            _duplicate_error(),
            "repeated a question",
        ),
        (
            QuizRejectionCategory.SEMANTIC_INVALID,
            "semantic",
            "semantic invalid",
        ),
    ],
)
async def test_quiz_rejection_uses_category_feedback_then_recovers(
    category: QuizRejectionCategory,
    first_effect: object,
    expected_feedback: str,
) -> None:
    request = _request()
    valid = await _valid_quiz()
    if first_effect == "semantic":
        invalid = valid.model_copy(update={"topic": "Boshqa mavzu"})
        first_effect = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=invalid))],
        )
    valid_completion = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=valid))],
    )
    outbound = AsyncMock(side_effect=[first_effect, valid_completion])
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=10,
        retry_base_delay_seconds=0,
    )
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(parse=outbound)),
        close=AsyncMock(),
    )
    reserve = AsyncMock()
    record = AsyncMock()
    provider.set_quiz_call_reserver(reserve)
    provider.set_quiz_rejection_recorder(record)

    result = await provider.generate_quiz(request)

    assert result == valid
    assert outbound.await_count == 2
    assert reserve.await_count == 2
    record.assert_awaited_once_with(category)
    correction = outbound.await_args_list[1].kwargs["messages"][-1]["content"]
    assert expected_feedback in correction.casefold()
    if category is QuizRejectionCategory.DUPLICATE:
        assert "every question unique" in correction.casefold()
        assert "options must be unique" not in correction.casefold()
    assert "TOP-SECRET-RAW-PREVIOUS-RESPONSE" not in correction


@pytest.mark.asyncio
async def test_four_invalid_quiz_responses_never_make_a_fifth_call_or_leak_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-live-RAW-PRIVATE-CONTENT"
    valid = await _valid_quiz()
    invalid = valid.model_copy(update={"topic": secret})
    invalid_completion = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=invalid))],
    )
    outbound = AsyncMock(side_effect=[invalid_completion] * 4)
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=10,
        retry_base_delay_seconds=0,
    )
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(parse=outbound)),
        close=AsyncMock(),
    )
    reserve = AsyncMock()
    record = AsyncMock()
    provider.set_quiz_call_reserver(reserve)
    provider.set_quiz_rejection_recorder(record)

    with caplog.at_level("INFO"), pytest.raises(Exception, match="budget exhausted"):
        await provider.generate_quiz(_request())

    assert outbound.await_count == 4
    assert reserve.await_count == 4
    assert record.await_count == 4
    assert secret not in caplog.text
