from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.ai.essay_contracts import EssayDocument, EssayGenerationRequest
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


async def test_mock_provider_generates_valid_essay_and_applies_requirements() -> None:
    request = EssayGenerationRequest(
        topic="Raqamli ta’lim",
        language="uz",
        length="short",
        additional_requirements="O‘zbekiston misollarini kiriting",
    )

    result = await MockAIProvider().generate_essay(request)

    assert isinstance(result, EssayDocument)
    assert result.language == "uz"
    corpus = result.model_dump_json()
    assert "O‘zbekiston" in corpus
    assert len(result.sections) >= 2


async def test_openai_provider_requests_strict_essay_schema() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    expected = await MockAIProvider().generate_essay(
        EssayGenerationRequest(topic="Ekologiya", language="uz", length="short")
    )
    request_structured = AsyncMock(return_value=expected)
    provider._request_structured = request_structured  # type: ignore[method-assign]
    request = EssayGenerationRequest(
        topic="Ekologiya",
        language="uz",
        length="short",
        additional_requirements="Mahalliy misollar",
    )

    try:
        result = await provider.generate_essay(request)
    finally:
        await provider.aclose()

    assert result == expected
    call = request_structured.await_args.kwargs
    assert call["response_format"] is EssayDocument
    assert call["response_validator"] is not None
    messages = list(call["messages"])
    assert "Mahalliy misollar" in messages[1]["content"]


async def test_structured_provider_retries_missing_parsed_object_within_bound() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        max_retries=1,
        retry_base_delay_seconds=0,
    )
    expected = await MockAIProvider().generate_essay(
        EssayGenerationRequest(topic="Ekologiya", language="uz", length="short")
    )
    missing = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=None))],
    )
    valid = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=expected))],
    )
    parse = AsyncMock(side_effect=[missing, valid])
    provider._client.chat.completions.parse = parse  # type: ignore[method-assign]

    try:
        result = await provider._request_structured(
            messages=[{"role": "system", "content": "test"}],
            response_format=EssayDocument,
        )
    finally:
        await provider.aclose()

    assert result == expected
    assert parse.await_count == 2
