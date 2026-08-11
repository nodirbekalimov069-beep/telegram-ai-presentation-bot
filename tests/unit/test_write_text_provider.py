from typing import Literal
from unittest.mock import AsyncMock

import pytest

from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.write_text_contracts import (
    WriteTextDocument,
    WriteTextGenerationRequest,
)


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_mock_provider_generates_request_bound_write_text(
    language: Literal["uz", "ru", "en"],
) -> None:
    request = WriteTextGenerationRequest(
        topic={
            "uz": "Mahalla hamjihatligi",
            "ru": "Командное сотрудничество",
            "en": "Responsible teamwork",
        }[language],
        text_type="article",
        style="formal",
        length="short",
        language=language,
        additional_requirements={
            "uz": "Amaliy hamkorlikni yoritish",
            "ru": "Показать практическое сотрудничество",
            "en": "Explain practical collaboration",
        }[language],
    )
    result = await MockAIProvider().generate_write_text(request)
    assert result.title == request.topic
    assert result.language == language
    assert result.text_type == "article"
    assert result.style == "formal"
    assert result.requirement_evidence
    assert result.references == []


@pytest.mark.parametrize("text_type", ["article", "post", "description", "letter"])
@pytest.mark.parametrize("style", ["simple", "formal", "academic", "creative"])
async def test_mock_provider_materializes_every_write_text_type_and_style(
    text_type: Literal["article", "post", "description", "letter"],
    style: Literal["simple", "formal", "academic", "creative"],
) -> None:
    request = WriteTextGenerationRequest(
        topic="Responsible teamwork",
        text_type=text_type,
        style=style,
        length="short",
        language="en",
        additional_requirements="Explain practical collaboration",
    )
    document = await MockAIProvider().generate_write_text(request)
    assert document.text_type == text_type
    assert document.style == style
    assert document.blocks


async def test_openai_provider_uses_strict_write_text_schema_and_validator() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    request = WriteTextGenerationRequest(
        topic="Mahalla hamjihatligi",
        text_type="article",
        style="formal",
        length="short",
        language="uz",
        additional_requirements="Amaliy hamkorlikni yoritish",
    )
    document = await MockAIProvider().generate_write_text(request)
    provider._request_structured = AsyncMock(return_value=document)  # type: ignore[method-assign]
    result = await provider.generate_write_text(request)
    assert result is document
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is WriteTextDocument
    assert call["response_validator"](document) is document
    assert request.topic in list(call["messages"])[1]["content"]
    with pytest.raises(AIContractError, match="language"):
        call["response_validator"](document.model_copy(update={"language": "en"}))
    await provider.aclose()
