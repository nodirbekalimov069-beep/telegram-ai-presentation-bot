from unittest.mock import AsyncMock

import pytest

from modules.ai.contracts import AIContractError
from modules.ai.document_contracts import (
    DocumentGenerationRequest,
    DocumentSelection,
    PreparedDocument,
)
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


async def test_mock_provider_generates_source_bound_document_without_authored_facts() -> None:
    request = DocumentGenerationRequest(
        document_type="application",
        details="Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, uch kunlik ta’til so‘rayman.",
        language="uz",
    )
    result = await MockAIProvider().generate_document(request)
    assert result.document_type == request.document_type
    assert result.language == request.language
    assert result.body_units == [
        "Toshkent davlat universiteti rektoriga.",
        "Men, Ali Valiyev, uch kunlik ta’til so‘rayman.",
    ]
    assert result.references == []


async def test_openai_provider_requests_selection_only_and_materializes_in_app() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=1,
    )
    request = DocumentGenerationRequest(
        document_type="official_letter",
        details="To the department head. Please schedule the meeting for 20 August 2026.",
        language="en",
    )
    selection = DocumentSelection(
        document_type="official_letter",
        language="en",
        source_indices=[0, 1],
    )
    provider._request_structured = AsyncMock(return_value=selection)  # type: ignore[method-assign]

    result = await provider.generate_document(request)

    assert isinstance(result, PreparedDocument)
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is DocumentSelection
    assert call["response_validator"](selection) is selection
    assert "source_indices" in next(iter(call["messages"]))["content"]
    with pytest.raises(AIContractError):
        call["response_validator"](selection.model_copy(update={"source_indices": [1, 0]}))
    await provider.aclose()
