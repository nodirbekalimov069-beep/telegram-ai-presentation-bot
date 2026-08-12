from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.providers.openai_compatible import _semantic_risk_source
from modules.ai.semantic_risk import SemanticRiskResult
from modules.ai.translation_contracts import (
    TranslationEvidence,
    TranslationGenerationRequest,
    TranslationSemanticAnalysis,
    TranslationSemanticFact,
    TranslationSemanticSegment,
    TranslationSegment,
    TranslationSelection,
    TranslationVerifierVerdict,
    attest_translation_verdict,
)


async def test_mock_translation_is_verified_and_preserves_atoms() -> None:
    request = TranslationGenerationRequest(
        text="Ali 2026-08-12 gacha 25 USD to‘lashi shart. https://example.com ga qarang.",
        target_language="en",
    )
    result = await MockAIProvider().generate_translation(request)
    assert result.target_language == "en"
    assert result.verifier_passed is True
    assert "2026-08-12" in result.segments[0].text
    assert "25 USD" in result.segments[0].text
    assert "https://example.com" in result.segments[0].text
    assert result.references == []


async def test_openai_translation_acceptance_uses_candidate_only() -> None:
    request = TranslationGenerationRequest(
        text="Ali 2026-08-12 gacha 25 USD to‘lashi shart.", target_language="en"
    )
    expected = await MockAIProvider().generate_translation(request)
    selection = TranslationSelection(
        source_language=expected.source_language,
        target_language=expected.target_language,
        segments=expected.segments,
    )

    risk_boundary = AsyncMock()
    risk_boundary.assess.return_value = SemanticRiskResult(0.01, 0.79, 0.20)
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=risk_boundary,
    )
    provider._request_structured = AsyncMock(return_value=selection)  # type: ignore[method-assign]

    result = await provider.generate_translation(request)

    assert result.verifier_passed
    calls = provider._request_structured.await_args_list  # type: ignore[attr-defined]
    assert [call.kwargs["response_format"] for call in calls] == [TranslationSelection]
    assert [call.kwargs.get("retry_invalid_response", True) for call in calls] == [True]
    assert all(call.kwargs["temperature"] == 0.0 for call in calls)
    assert request.text in list(calls[0].kwargs["messages"])[1]["content"]

    await provider.aclose()


async def test_provider_path_rejects_buy_sell_lie_even_with_matching_forged_graphs_and_all_true_verdict() -> (
    None
):
    request = TranslationGenerationRequest(
        text="Ali Vali uchun avtomobil sotib oldi.", target_language="en"
    )
    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0, kind="paragraph", text="Ali sold a vehicle for Vali."
            )
        ],
    )
    forged_graph = TranslationSemanticAnalysis(
        language="uz",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[TranslationSemanticFact(subject="fixture", predicate="fixture")],
            )
        ],
    )
    verdict = TranslationVerifierVerdict(
        passed=True,
        completeness=True,
        non_invention=True,
        roles_and_ownership=True,
        polarity_modality_quantifiers=True,
        conditions_and_chronology=True,
        number_entity_associations=True,
        quote_attribution=True,
        target_language=True,
        structure=True,
        evidence=[
            TranslationEvidence(
                source_index=0,
                source_excerpt=request.text,
                candidate_excerpt=selection.segments[0].text,
            )
        ],
    )
    verdict = attest_translation_verdict(
        verdict,
        request=request,
        selection=selection,
        source_semantics=forged_graph,
        candidate_semantics=forged_graph.model_copy(update={"language": "en"}),
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="test-key", model="test", max_retries=0
    )
    provider._request_structured = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            selection,
            forged_graph,
            forged_graph.model_copy(update={"language": "en"}),
            verdict,
        ]
    )

    with pytest.raises(AIContractError):
        await provider.generate_translation(request)

    await provider.aclose()


async def test_app_owned_risk_failure_gets_one_bounded_candidate_retry() -> None:
    request = TranslationGenerationRequest(
        text="Ali Vali uchun avtomobil sotib oldi.", source_language="uz", target_language="en"
    )
    bad = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0, kind="paragraph", text="Ali sold a vehicle for Vali."
            )
        ],
    )
    good = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0, kind="paragraph", text="Ali bought a vehicle for Vali."
            )
        ],
    )

    def completion(parsed: TranslationSelection) -> SimpleNamespace:
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=parsed))],
        )

    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=1,
        retry_base_delay_seconds=0,
        semantic_risk_boundary=AsyncMock(
            assess=AsyncMock(return_value=SemanticRiskResult(0.01, 0.79, 0.20))
        ),
    )
    parse = AsyncMock(side_effect=[completion(bad), completion(good)])
    provider._client.chat.completions.parse = parse  # type: ignore[method-assign]

    result = await provider.generate_translation(request)

    assert result.segments[0].text == good.segments[0].text
    assert parse.await_count == 2
    retry_messages = parse.await_args_list[1].kwargs["messages"]
    assert "contract validation" in retry_messages[-1]["content"]
    await provider.aclose()


async def test_independent_risk_boundary_rejects_love_hate_outside_action_dictionary() -> None:
    request = TranslationGenerationRequest(
        text="Alice loves Bob.", source_language="en", target_language="ru"
    )
    candidate = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[
            TranslationSegment(source_index=0, kind="paragraph", text="Alice ненавидит Bob.")
        ],
    )
    risk_boundary = AsyncMock()
    risk_boundary.assess.return_value = SemanticRiskResult(
        contradiction_probability=0.7787,
        entailment_probability=0.0456,
        neutral_probability=0.1757,
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=risk_boundary,
    )
    provider._request_structured = AsyncMock(return_value=candidate)  # type: ignore[method-assign]

    with pytest.raises(AIContractError, match="semantic risk validation"):
        await provider.generate_translation(request)
    assert risk_boundary.assess.await_count == 4
    assert risk_boundary.assess.await_args_list[0].args == (
        request.text,
        candidate.segments[0].text,
    )
    assert risk_boundary.assess.await_args_list[1].args == (
        candidate.segments[0].text,
        request.text,
    )
    await provider.aclose()


@pytest.mark.parametrize("mode", ["missing", "timeout"])
async def test_translation_fails_closed_when_independent_risk_boundary_is_unavailable(
    mode: str,
) -> None:
    boundary = None
    if mode == "timeout":
        boundary = AsyncMock()
        boundary.assess.side_effect = TimeoutError()
    request = TranslationGenerationRequest(
        text="Alice loves Bob.", source_language="en", target_language="ru"
    )
    candidate = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text="Alice любит Bob.")],
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=candidate)  # type: ignore[method-assign]

    with pytest.raises(AIContractError, match=r"risk boundary|semantic risk validation"):
        await provider.generate_translation(request)
    await provider.aclose()


async def test_independent_risk_boundary_rejects_low_confidence_candidate() -> None:
    request = TranslationGenerationRequest(
        text="Alice loves Bob.", source_language="en", target_language="ru"
    )
    candidate = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text="Alice знает Bob.")],
    )
    boundary = AsyncMock()
    boundary.assess.return_value = SemanticRiskResult(0.10, 0.30, 0.60)
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    with pytest.raises(AIContractError, match="semantic risk validation"):
        await provider.generate_translation(request)
    assert boundary.assess.await_count == 4
    await provider.aclose()


async def test_non_neutral_exact_pass_through_cannot_bypass_translation_boundary() -> None:
    source = "Ephemeral dislocations reshape contingencies."
    request = TranslationGenerationRequest(text=source, source_language="en", target_language="uz")
    unchanged = TranslationSelection(
        source_language="en",
        target_language="uz",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=source)],
    )
    boundary = AsyncMock()
    boundary.assess.return_value = SemanticRiskResult(0.01, 0.99, 0.0)
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=unchanged)  # type: ignore[method-assign]

    with pytest.raises(AIContractError, match="semantic risk validation"):
        await provider.generate_translation(request)
    assert boundary.assess.await_count == 0
    await provider.aclose()


@pytest.mark.parametrize("source", ["Cats Sleep", "Birds Fly"])
async def test_unknown_two_word_title_case_prose_cannot_exact_pass_as_neutral(
    source: str,
) -> None:
    request = TranslationGenerationRequest(text=source, source_language="en", target_language="uz")
    unchanged = TranslationSelection(
        source_language="en",
        target_language="uz",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=source)],
    )
    boundary = AsyncMock()
    boundary.assess.return_value = SemanticRiskResult(0.01, 0.99, 0.0)
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=unchanged)  # type: ignore[method-assign]

    with pytest.raises(AIContractError, match="semantic risk validation"):
        await provider.generate_translation(request)
    assert boundary.assess.await_count == 0
    await provider.aclose()


@pytest.mark.parametrize("source", ["Ali Vali", "Али Вали", "OpenAI", "NASA"])
async def test_strict_app_owned_neutral_grammar_allows_exact_pass_through(source: str) -> None:
    request = TranslationGenerationRequest(text=source, source_language="uz", target_language="en")
    unchanged = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=source)],
    )
    boundary = AsyncMock()
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=unchanged)  # type: ignore[method-assign]

    document = await provider.generate_translation(request)
    assert document.segments[0].text == source
    assert boundary.assess.await_count == 0
    await provider.aclose()


async def test_reverse_independent_nli_rejects_general_clause_omission() -> None:
    request = TranslationGenerationRequest(
        text="The vessel glimmers, and the meadow rustles.",
        source_language="en",
        target_language="ru",
    )
    omitted = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text="Судно мерцает.")],
    )
    boundary = AsyncMock()
    boundary.assess.side_effect = [
        SemanticRiskResult(0.01, 0.91, 0.08),
        SemanticRiskResult(0.01, 0.20, 0.79),
        SemanticRiskResult(0.01, 0.91, 0.08),
        SemanticRiskResult(0.01, 0.20, 0.79),
    ]
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test",
        max_retries=0,
        semantic_risk_boundary=boundary,
    )
    provider._request_structured = AsyncMock(return_value=omitted)  # type: ignore[method-assign]

    with pytest.raises(AIContractError, match="semantic risk validation"):
        await provider.generate_translation(request)
    assert boundary.assess.await_count == 4
    assert boundary.assess.await_args_list[0].args == (
        request.text,
        omitted.segments[0].text,
    )
    assert boundary.assess.await_args_list[1].args == (
        omitted.segments[0].text,
        request.text,
    )
    await provider.aclose()


def test_uzbek_cyrillic_is_normalized_only_for_independent_nli_input() -> None:
    assert _semantic_risk_source("Ўзбекистон тарихи", "uz") == "O‘zbekiston tarixi"
    assert _semantic_risk_source("Ўзбекистон тарихи", "ru") == "Ўзбекистон тарихи"
