from typing import Literal
from unittest.mock import AsyncMock

import pytest

from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.rewrite_contracts import RewriteGenerationRequest, RewriteSelection


@pytest.mark.parametrize(
    ("language", "source"),
    [
        (
            "uz",
            "Jamoa vazifalarni umumiy reja bilan muvofiqlashtiradi va har bir natijani tekshiradi.",
        ),
        (
            "ru",
            "Команда координирует задачи по общему плану и проверяет каждый результат работы.",
        ),
        (
            "en",
            "The team coordinates tasks through a shared plan and verifies each result before delivery.",
        ),
    ],
)
@pytest.mark.parametrize("mode", ["simple", "formal", "academic", "shorten", "clarify"])
async def test_mock_provider_generates_source_bound_rewrite_for_every_language_and_mode(
    language: Literal["uz", "ru", "en"],
    source: str,
    mode: Literal["simple", "formal", "academic", "shorten", "clarify"],
) -> None:
    request = RewriteGenerationRequest(text=source, mode=mode)

    document = await MockAIProvider().generate_rewrite(request)

    assert document.language == language
    assert document.mode == mode
    assert len(document.segments) == 1
    assert document.segments[0].source_index == 0
    assert document.segments[0].text != source
    assert document.references == []


async def test_openai_provider_uses_strict_rewrite_selection_and_request_validator() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    request = RewriteGenerationRequest(
        text="The team coordinates tasks through a shared plan and verifies each result.",
        mode="formal",
    )
    expected = await MockAIProvider().generate_rewrite(request)
    selection = RewriteSelection(
        language=expected.language,
        mode=expected.mode,
        segments=expected.segments,
    )
    provider._request_structured = AsyncMock(return_value=selection)  # type: ignore[method-assign]

    result = await provider.generate_rewrite(request)

    assert result.segments == expected.segments
    kwargs = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["response_format"] is RewriteSelection
    assert kwargs["retry_invalid_response"] is False
    assert kwargs["temperature"] == 0.0
    validator = kwargs["response_validator"]
    assert validator(selection).segments == expected.segments
    assert request.text in list(kwargs["messages"])[1]["content"]
    await provider.aclose()
