import pytest

from modules.ai.contracts import AIContractError
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
    materialize_and_validate_translation,
)


def _verdict(
    request: TranslationGenerationRequest,
    selection: TranslationSelection,
    *,
    source_excerpt: str | None = None,
    candidate_excerpt: str | None = None,
) -> TranslationVerifierVerdict:
    return TranslationVerifierVerdict(
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
                source_excerpt=source_excerpt or request.text,
                candidate_excerpt=candidate_excerpt or selection.segments[0].text,
            )
        ],
    )


def test_candidate_must_deterministically_match_target_language() -> None:
    request = TranslationGenerationRequest(
        text="Bu matn foydalanuvchi uchun aniq ma’lumot va natijani tushuntiradi.",
        target_language="en",
    )
    untranslated = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text=request.text,
            )
        ],
    )
    with pytest.raises(AIContractError, match="target language"):
        materialize_and_validate_translation(
            untranslated,
            verdict=_verdict(request, untranslated),
            request=request,
        )


def test_numbered_list_marker_cannot_be_changed_to_a_bullet() -> None:
    request = TranslationGenerationRequest(
        text="1) Bu birinchi vazifa va u bugun bajarilishi kerak.",
        target_language="en",
    )
    changed = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="list_item",
                text="1. This is the first task and it must be completed today.",
            )
        ],
    )
    with pytest.raises(AIContractError, match="list marker"):
        materialize_and_validate_translation(
            changed,
            verdict=_verdict(request, changed),
            request=request,
        )


def test_verifier_evidence_cannot_be_only_name_marker_stuffing() -> None:
    request = TranslationGenerationRequest(
        text=(
            "Ali Vali uchun muhim hisobotni tayyorladi, keyin Nodir uni "
            "mijozga faqat tasdiq olingandan so‘ng yubordi."
        ),
        target_language="en",
    )
    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text=(
                    "After Ali Vali Nodir prepared the important report, it was then sent "
                    "to the client only after approval was obtained."
                ),
            )
        ],
    )
    source_semantics = TranslationSemanticAnalysis(
        language="uz",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[TranslationSemanticFact(subject="fixture", predicate="same")],
            )
        ],
    )
    candidate_semantics = source_semantics.model_copy(update={"language": "en"})
    verdict = attest_translation_verdict(
        _verdict(
            request,
            selection,
            source_excerpt="Ali Vali",
            candidate_excerpt="Ali Vali",
        ),
        request=request,
        selection=selection,
        source_semantics=source_semantics,
        candidate_semantics=candidate_semantics,
    )
    with pytest.raises(AIContractError, match="substantive"):
        materialize_and_validate_translation(
            selection,
            verdict=verdict,
            request=request,
        )
