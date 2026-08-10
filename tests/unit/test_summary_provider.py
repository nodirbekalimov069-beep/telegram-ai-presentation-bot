from typing import Literal
from unittest.mock import AsyncMock

import pytest

from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.summary_contracts import (
    SummaryGenerationRequest,
    SummarySelection,
    SummarySelectionSection,
)


@pytest.mark.parametrize(
    ("language", "source", "requirements"),
    [
        (
            "uz",
            "Algoritmik fikrlash vazifani qismlarga ajratadi. Inson nazorati yechimni "
            "tekshiradi va amaliy xavfni kamaytiradi.",
            "Inson nazorati rolini alohida tushuntiring.",
        ),
        (
            "ru",
            "Алгоритмическое мышление разделяет задачу на части. Человеческий контроль "
            "проверяет решение и снижает практический риск.",
            "Отдельно объясните роль человеческого контроля.",
        ),
        (
            "en",
            "Algorithmic thinking separates a task into coherent parts. Human oversight "
            "checks the solution and reduces practical risk.",
            "Explain the role of human oversight in practical verification.",
        ),
    ],
)
async def test_mock_provider_generates_valid_source_bound_summary(
    language: Literal["uz", "ru", "en"],
    source: str,
    requirements: str,
) -> None:
    request = SummaryGenerationRequest(
        source=source,
        detail="short",
        language=language,
        additional_requirements=requirements,
    )

    result = await MockAIProvider().generate_summary(request)

    assert result.language == language
    assert len(result.sections) >= 2
    assert result.requirement_evidence
    assert result.references == []
    assert result.title == {"uz": "Konspekt", "ru": "Конспект", "en": "Summary"}[language]
    corpus = "\n".join([result.overview, result.conclusion, *result.requirement_evidence])
    assert all(line in source for line in corpus.splitlines())
    assert requirements not in corpus


async def test_openai_provider_uses_strict_summary_schema_and_final_validator() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    request = SummaryGenerationRequest(
        source=(
            "Algoritmik fikrlash vazifani bosqichlarga ajratadi. Inson nazorati natijani "
            "tekshiradi va amaliy xavfni kamaytiradi."
        ),
        detail="short",
        language="uz",
    )
    selection = SummarySelection(
        language="uz",
        detail="short",
        include_requirements=False,
        overview_indices=[0, 1],
        key_point_indices=[0, 1, 0],
        sections=[
            SummarySelectionSection(sentence_indices=[0]),
            SummarySelectionSection(sentence_indices=[1]),
        ],
        conclusion_indices=[1, 0],
    )
    provider._request_structured = AsyncMock(return_value=selection)  # type: ignore[method-assign]

    result = await provider.generate_summary(request)

    assert result.language == request.language
    assert result.references == []
    assert all(
        sentence in request.source
        for section in result.sections
        for paragraph in section.paragraphs
        for sentence in paragraph.splitlines()
    )
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is SummarySelection
    assert call["response_validator"](selection) is selection
    message_content = list(call["messages"])[1]["content"]
    assert all(sentence in message_content for sentence in request.source.split(". ") if sentence)

    invalid = selection.model_copy(update={"overview_indices": [99]})
    with pytest.raises(AIContractError, match="out-of-range"):
        call["response_validator"](invalid)
    await provider.aclose()
