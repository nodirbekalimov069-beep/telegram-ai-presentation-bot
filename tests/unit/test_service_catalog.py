import pytest

from modules.service_orders.catalog import (
    PRICING_CATEGORIES,
    SERVICE_CATALOG,
    FieldKind,
    ServiceType,
    validate_service_request,
)


EXPECTED_FIELDS = {
    ServiceType.ESSAY: ("topic", "language", "length", "requirements"),
    ServiceType.INDEPENDENT_WORK: ("subject", "topic", "length", "language", "requirements"),
    ServiceType.COURSE_WORK: (
        "subject",
        "topic",
        "sections",
        "length",
        "language",
        "requirements",
    ),
    ServiceType.SUMMARY: ("source", "detail", "language"),
    ServiceType.QUIZ: (
        "topic",
        "question_count",
        "difficulty",
        "option_count",
        "answer_key",
        "language",
    ),
    ServiceType.WRITE_TEXT: ("topic", "text_type", "style", "length", "language"),
    ServiceType.REWRITE: ("text", "mode"),
    ServiceType.TRANSLATE: ("text", "target_language"),
    ServiceType.DOCUMENT: ("document_type", "details"),
}


def test_catalog_defines_all_requested_services_and_fields() -> None:
    assert set(SERVICE_CATALOG) == set(ServiceType) == set(EXPECTED_FIELDS)
    for service_type, expected_fields in EXPECTED_FIELDS.items():
        definition = SERVICE_CATALOG[service_type]
        assert tuple(field.name for field in definition.fields) == expected_fields
        assert definition.production_ready is False
        assert definition.large_job is (service_type is ServiceType.COURSE_WORK)


def test_catalog_validation_normalizes_valid_payload_and_rejects_unknown_fields() -> None:
    payload = validate_service_request(
        ServiceType.QUIZ,
        {
            "topic": "Sun’iy intellekt",
            "question_count": "20",
            "difficulty": "medium",
            "option_count": "4",
            "answer_key": "yes",
            "language": "uz",
        },
    )

    assert payload["question_count"] == 20
    assert payload["option_count"] == 4
    assert payload["answer_key"] is True
    with pytest.raises(ValueError, match="unknown fields"):
        validate_service_request(ServiceType.QUIZ, {**payload, "admin": True})


def test_catalog_rejects_oversized_or_invalid_values() -> None:
    with pytest.raises(ValueError, match="text"):
        validate_service_request(
            ServiceType.REWRITE,
            {"text": "x" * 4001, "mode": "formal"},
        )
    with pytest.raises(ValueError, match="question_count"):
        validate_service_request(
            ServiceType.QUIZ,
            {
                "topic": "AI",
                "question_count": "500",
                "difficulty": "medium",
                "option_count": "4",
                "answer_key": "yes",
                "language": "uz",
            },
        )


def test_choice_fields_have_short_deterministic_values() -> None:
    for definition in SERVICE_CATALOG.values():
        for field in definition.fields:
            if field.kind is FieldKind.CHOICE:
                assert field.options
                assert len(field.options) == len(set(field.options))
                assert all(value.isascii() and len(value) <= 20 for value in field.options)


def test_pricing_catalog_is_centralized_and_does_not_invent_unknown_prices() -> None:
    assert tuple(item.code for item in PRICING_CATEGORIES) == (
        "presentation",
        "written_work",
        "ai_text",
        "documents",
    )
    presentation, *unknown_price_categories = PRICING_CATEGORIES
    assert presentation.price_source == "database_plans"
    assert all(item.price_uzs is None for item in unknown_price_categories)
