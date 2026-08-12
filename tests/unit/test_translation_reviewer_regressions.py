import pytest

from modules.ai.contracts import AIContractError
from modules.ai.translation_contracts import (
    TranslationDocument,
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
    source_language_for_request,
    source_translation_segments,
    translation_protected_association_score,
    validate_translation_selection,
)
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType, validate_service_request


def _accept_with_all_true_verdict(
    source: str,
    target: str,
    candidates: list[tuple[str, str]],
) -> None:
    request = TranslationGenerationRequest(text=source, target_language=target)  # type: ignore[arg-type]
    selection = TranslationSelection(
        source_language="uz",
        target_language=target,  # type: ignore[arg-type]
        segments=[
            TranslationSegment(source_index=index, kind=kind, text=text)  # type: ignore[arg-type]
            for index, (kind, text) in enumerate(candidates)
        ],
    )
    source_parts = source_translation_segments(source)
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
                source_index=index,
                source_excerpt=source_parts[index][1],
                candidate_excerpt=text,
            )
            for index, (_, text) in enumerate(candidates)
        ],
    )
    source_semantics = TranslationSemanticAnalysis(
        language="uz",
        segments=[
            TranslationSemanticSegment(
                source_index=index,
                facts=[TranslationSemanticFact(subject="fixture", predicate=str(index))],
            )
            for index in range(len(selection.segments))
        ],
    )
    verdict = attest_translation_verdict(
        verdict,
        request=request,
        selection=selection,
        source_semantics=source_semantics,
        candidate_semantics=source_semantics.model_copy(update={"language": target}),
    )
    materialize_and_validate_translation(selection, verdict=verdict, request=request)


@pytest.mark.parametrize(
    "candidate",
    [
        "Ali did not approve the report for Vali.",
        "Ali approved all reports for Vali.",
        "Ali may approve the report for Vali.",
        "Vali approved the report for Ali.",
        "Ali approved the report for Vali because Nodir ordered it.",
        "According to Nodir, Ali approved the report for Vali.",
        "Ali Vali Nodir the report and the report for the client after approval.",
        "Salom dunyo",
        '"""The report for Vali was approved by Ali."""',
        "“The report for Vali was approved by Ali.”",
        "Translation: The report for Vali was approved by Ali.",
        'Ali approved the report for Vali. {"translation":"extra"}',
        "Ali approved the report for Vali. I cannot translate this source.",
        "Ali approved the report for Vali. hola mundo.",
    ],
    ids=(
        "negation",
        "quantifier",
        "modality",
        "role-swap",
        "causality-invention",
        "attribution-invention",
        "marker-stuffing",
        "wrong-target-tie",
        "triple-quote-wrapper",
        "smart-quote-wrapper",
        "translation-label-wrapper",
        "suffix-json-wrapper",
        "suffix-refusal",
        "lowercase-multilingual-stuffing",
    ),
)
def test_all_true_verdict_cannot_bypass_relational_validation(candidate: str) -> None:
    with pytest.raises(AIContractError):
        _accept_with_all_true_verdict(
            "Ali Vali uchun hisobotni tasdiqladi.", "en", [("paragraph", candidate)]
        )


@pytest.mark.parametrize(
    ("source", "candidates"),
    [
        (
            "Ali 25 USD to‘lashi kerak va Vali 50 EUR to‘lashi kerak.",
            [("paragraph", "Ali must pay 50 EUR and Vali must pay 25 USD.")],
        ),
        (
            "Agar Ali kelsa, Vali hisobotni yuborishi kerak, lekin ruxsat bo‘lmasa yubormasligi shart.",
            [("paragraph", "Vali must send the report.")],
        ),
        (
            "Ali hisobotni yuborishdan oldin Vali uni tasdiqlashi kerak.",
            [("paragraph", "Vali must approve it after Ali sends the report.")],
        ),
        (
            "1) Bu birinchi vazifa va uni Ali bajarishi kerak.\n2) Bu ikkinchi vazifa va uni Vali bajarishi kerak.",
            [
                ("list_item", "1) Vali must complete the second task."),
                ("list_item", "2) Ali must complete the first task."),
            ],
        ),
        (
            "Ali uchun | 25 USD\nVali uchun | 50 EUR",
            [("table_row", "25 USD | for Ali"), ("table_row", "50 EUR | for Vali")],
        ),
        (
            '"Ali hisobotni tasdiqladi", dedi Vali.',
            [("quote", "“Vali approved the report,” said Ali.")],
        ),
    ],
    ids=(
        "amount-currency-reassociation",
        "condition-exception-removal",
        "chronology-reversal",
        "list-content-reorder",
        "table-cell-reorder",
        "quote-owner-swap",
    ),
)
def test_all_true_verdict_cannot_bypass_structured_semantics(
    source: str, candidates: list[tuple[str, str]]
) -> None:
    with pytest.raises(AIContractError):
        _accept_with_all_true_verdict(source, "en", candidates)


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("Hello world", "ru"),
        ("Project Atlas", "ru"),
        ("Salom dunyo", "en"),
        ("Loyiha Atlas", "en"),
        ("Alice Johnson", "ru"),
        ("Alisher Navoiy", "en"),
        ("cats sleep quietly", "ru"),
        ("Good morning", "ru"),
        ("Assalomu alaykum", "en"),
    ],
)
def test_short_natural_source_and_proper_name_positive_controls(text: str, target: str) -> None:
    assert TranslationGenerationRequest(text=text, target_language=target).text == text  # type: ignore[arg-type]


def test_public_catalog_does_not_stringify_non_string_translation_text() -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_service_request(
            ServiceType.TRANSLATE,
            {"text": ["This", "is", "English"], "target_language": "ru"},
        )


def test_protected_association_score_rejects_amount_owner_swap() -> None:
    request = TranslationGenerationRequest(
        text="Ali 25 USD to‘lashi kerak va Vali 50 EUR to‘lashi kerak.",
        target_language="en",
    )
    forged = TranslationDocument(
        title="Translation",
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text="Ali must pay 50 EUR and Vali must pay 25 USD.",
            )
        ],
        verifier_passed=True,
        references=[],
    )
    assert translation_protected_association_score(request, forged) == 0.0


def test_isolated_semantic_graphs_reject_unmarked_predicate_substitution() -> None:
    request = TranslationGenerationRequest(
        text="Ali Vali uchun hisobotni tasdiqladi.", target_language="en"
    )
    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text="Ali destroyed the report for Vali.",
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
    source_semantics = TranslationSemanticAnalysis(
        language="uz",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[
                    TranslationSemanticFact(
                        subject="Ali", predicate="approve", object="report for Vali"
                    )
                ],
            )
        ],
    )
    candidate_semantics = TranslationSemanticAnalysis(
        language="en",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[
                    TranslationSemanticFact(
                        subject="Ali", predicate="destroy", object="report for Vali"
                    )
                ],
            )
        ],
    )
    with pytest.raises(AIContractError, match="semantic graphs"):
        attest_translation_verdict(
            verdict,
            request=request,
            selection=selection,
            source_semantics=source_semantics,
            candidate_semantics=candidate_semantics,
        )


def test_candidate_semantic_analyzer_rejects_wrong_target_language() -> None:
    request = TranslationGenerationRequest(text="Это русский текст", target_language="uz")
    selection = TranslationSelection(
        source_language="ru",
        target_language="uz",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text="Good morning")],
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
                candidate_excerpt="Good morning",
            )
        ],
    )
    source_semantics = TranslationSemanticAnalysis(
        language="ru",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[TranslationSemanticFact(subject="speaker", predicate="greet")],
            )
        ],
    )
    candidate_semantics = TranslationSemanticAnalysis(
        language="en",
        segments=[
            TranslationSemanticSegment(
                source_index=0,
                facts=[TranslationSemanticFact(subject="speaker", predicate="greet")],
            )
        ],
    )
    with pytest.raises(AIContractError, match="wrong language"):
        attest_translation_verdict(
            verdict,
            request=request,
            selection=selection,
            source_semantics=source_semantics,
            candidate_semantics=candidate_semantics,
        )


@pytest.mark.parametrize(
    ("source_language", "target_language", "source", "candidate"),
    [
        ("uz", "en", "Ali Vali uchun avtomobil sotib oldi.", "Ali sold a vehicle for Vali."),
        ("uz", "en", "Ali Valiga kitob berdi.", "Ali received a book from Vali."),
        ("uz", "en", "Narx oshdi.", "The price decreased."),
        ("uz", "en", "Ali xonaga kirdi.", "Ali left the room."),
        ("uz", "en", "Ali chiroqni yoqdi.", "Ali turned off the light."),
        ("uz", "en", "Ali oldin keldi.", "Ali arrived after."),
        ("uz", "en", "Hisobot bor.", "The report is absent."),
        ("uz", "en", "Ali kirishi mumkin.", "Ali cannot enter."),
        ("uz", "en", "Ali hisobotni tasdiqladi.", "Ali rejected the report."),
        ("ru", "en", "Али выиграл матч.", "Ali lost the match."),
        ("ru", "en", "Али отправил письмо.", "Ali received the letter."),
        ("ru", "en", "Али дал Вали в долг.", "Ali borrowed from Vali."),
    ],
    ids=(
        "buy-sell",
        "give-receive",
        "increase-decrease",
        "enter-exit",
        "turn-on-off",
        "before-after",
        "present-absent",
        "may-cannot",
        "approve-reject",
        "win-lose",
        "send-receive",
        "lend-borrow",
    ),
)
def test_app_owned_contradiction_gate_rejects_opposite_actions(
    source_language: str, target_language: str, source: str, candidate: str
) -> None:
    request = TranslationGenerationRequest(
        text=source,
        source_language=source_language,  # type: ignore[arg-type]
        target_language=target_language,  # type: ignore[arg-type]
    )
    selection = TranslationSelection(
        source_language=source_language,  # type: ignore[arg-type]
        target_language=target_language,  # type: ignore[arg-type]
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=candidate)],
    )
    with pytest.raises(AIContractError):
        validate_translation_selection(selection, request=request)


@pytest.mark.parametrize(
    ("source_language", "target_language", "text"),
    [
        ("en", "ru", "we deliver quality work"),
        ("en", "ru", "people value reliable service"),
        ("uz", "en", "Men bugun maktabga bordim"),
        ("uz", "en", "U yaxshi ishlaydi"),
        ("ru", "uz", "Это естественный русский текст"),
        ("uz", "en", "oziq ovqat sifatini nazorat qilamiz"),
    ],
)
def test_explicit_source_language_accepts_natural_out_of_dictionary_text(
    source_language: str, target_language: str, text: str
) -> None:
    request = TranslationGenerationRequest(
        text=text,
        source_language=source_language,  # type: ignore[arg-type]
        target_language=target_language,  # type: ignore[arg-type]
    )
    assert request.source_language == source_language


@pytest.mark.parametrize(
    "text", ["Ali Vali", "OpenAI", "Samarkand", "API-2026", "user@example.com"]
)
def test_language_neutral_source_and_candidate_can_pass_through(text: str) -> None:
    request = TranslationGenerationRequest(text=text, source_language="uz", target_language="en")
    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=text)],
    )
    assert validate_translation_selection(selection, request=request) is selection


def test_production_catalog_requires_explicit_translation_source_language() -> None:
    fields = [field.name for field in SERVICE_CATALOG[ServiceType.TRANSLATE].fields]
    assert fields == ["source_language", "text", "target_language"]


def test_title_case_sentence_is_not_language_neutral_pass_through() -> None:
    request = TranslationGenerationRequest(
        text="All Cats Sleep", source_language="en", target_language="ru"
    )
    selection = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text="All Cats Sleep")],
    )
    with pytest.raises(AIContractError):
        validate_translation_selection(selection, request=request)


@pytest.mark.parametrize(
    ("source_language", "target_language", "source", "candidate"),
    [
        (
            "uz",
            "en",
            "Texnologik yangilanishlar jamiyatni o‘zgartiradi.",
            "Technology transforms society.",
        ),
        (
            "en",
            "uz",
            "Ephemeral dislocations reshape contingencies.",
            "Muvaqqat nomutanosibliklar ehtiyot choralarini qayta shakllantiradi.",
        ),
    ],
)
def test_target_language_uncertainty_does_not_reject_natural_out_of_dictionary_text(
    source_language: str, target_language: str, source: str, candidate: str
) -> None:
    request = TranslationGenerationRequest(
        text=source,
        source_language=source_language,  # type: ignore[arg-type]
        target_language=target_language,  # type: ignore[arg-type]
    )
    selection = TranslationSelection(
        source_language=source_language,  # type: ignore[arg-type]
        target_language=target_language,  # type: ignore[arg-type]
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=candidate)],
    )
    assert validate_translation_selection(selection, request=request) is selection


def test_russian_pered_preserves_before_chronology_marker() -> None:
    request = TranslationGenerationRequest(
        text="Modern research teams evaluate complex evidence before making careful decisions.",
        source_language="en",
        target_language="ru",
    )
    selection = TranslationSelection(
        source_language="en",
        target_language="ru",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text=(
                    "Современные исследовательские группы оценивают сложные доказательства "
                    "перед принятием взвешенных решений."
                ),
            )
        ],
    )
    assert validate_translation_selection(selection, request=request) is selection


def test_explicit_uzbek_source_accepts_natural_cyrillic_uzbek() -> None:
    request = TranslationGenerationRequest(
        text="Ўзбекистон тарихи ва маданияти бой.",
        source_language="uz",
        target_language="en",
    )
    assert source_language_for_request(request) == "uz"

    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text="The history and culture of Uzbekistan are rich.",
            )
        ],
    )
    assert validate_translation_selection(selection, request=request) is selection


@pytest.mark.parametrize(
    "candidate",
    [
        "Ali approved the report.",
        "Ali approved the report. Vali sold a car.",
    ],
    ids=("important-sentence-omitted", "source-absent-fact-added"),
)
def test_app_owned_gate_rejects_sentence_omission_or_invention(candidate: str) -> None:
    request = TranslationGenerationRequest(
        text="Ali hisobotni tasdiqladi. Vali xatni yubordi.",
        source_language="uz",
        target_language="en",
    )
    selection = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[TranslationSegment(source_index=0, kind="paragraph", text=candidate)],
    )
    with pytest.raises(AIContractError):
        validate_translation_selection(selection, request=request)
