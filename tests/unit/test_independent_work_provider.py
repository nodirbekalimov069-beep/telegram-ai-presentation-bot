from unittest.mock import AsyncMock

import pytest

from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
    materialize_and_validate_independent_work,
)
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


async def test_mock_provider_generates_valid_independent_work_and_applies_requirements() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Pedagogika",
        topic="Interfaol ta’lim",
        language="uz",
        length="medium",
        additional_requirements="Xulosada hamkorlikdagi o‘rganish iborasini aynan ishlat.",
    )

    result = await MockAIProvider().generate_independent_work(request)

    assert result.subject == "Pedagogika"
    assert result.language == "uz"
    assert len(result.sections) >= 4
    assert "hamkorlikdagi o‘rganish" in result.conclusion
    assert result.references == []


async def test_openai_provider_uses_strict_independent_work_schema() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    expected = await MockAIProvider().generate_independent_work(
        IndependentWorkGenerationRequest(
            subject="Informatika",
            topic="Algoritmlar",
            language="uz",
            length="short",
        )
    )
    provider._request_structured = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
    )

    result = await provider.generate_independent_work(request)

    assert result is expected
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is IndependentWorkDocument
    assert call["response_validator"](expected) is expected
    messages = list(call["messages"])
    assert messages[0]["role"] == "system"
    assert "Informatika" in messages[1]["content"]
    await provider.aclose()


async def test_openai_provider_materializes_requirement_evidence_verbatim() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
        additional_requirements="Xulosada xavfsiz avtomatlashtirishni tushuntir.",
    )
    generated = await MockAIProvider().generate_independent_work(request)
    evidence = generated.requirement_evidence[0]
    payload = generated.model_dump()
    payload["conclusion"] = (
        "Algoritmlar mavzusi bo‘yicha xulosa nazariy va amaliy jihatlarni bog‘laydi. "
        "Yakuniy mulohaza mas’uliyatli qaror, inson nazorati va ehtiyotkor tatbiq etish "
        "zarurligini mavzu doirasida izchil umumlashtiradi."
    )
    generated_without_excerpt = IndependentWorkDocument.model_validate(payload)

    result = materialize_and_validate_independent_work(
        generated_without_excerpt,
        request=request,
    )

    assert evidence in result.conclusion
    assert result.conclusion.endswith(evidence)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"max_retries": -1},
        {"max_retries": 11},
        {"retry_base_delay_seconds": -1},
        {"retry_base_delay_seconds": float("nan")},
    ],
)
def test_openai_provider_rejects_unbounded_timeout_and_retry_values(
    overrides: dict[str, float | int],
) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            api_key="test-key",
            model="test-model",
            **overrides,
        )
