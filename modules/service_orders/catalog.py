"""Typed catalog and validation for non-presentation AI services."""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ServiceType(StrEnum):
    ESSAY = "essay"
    INDEPENDENT_WORK = "independent"
    COURSE_WORK = "course"
    SUMMARY = "summary"
    QUIZ = "quiz"
    WRITE_TEXT = "write"
    REWRITE = "rewrite"
    TRANSLATE = "translate"
    DOCUMENT = "document"


class FieldKind(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    CHOICE = "choice"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    prompt_key: str
    kind: FieldKind = FieldKind.TEXT
    minimum: int = 1
    maximum: int = 500
    options: tuple[str, ...] = ()
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_type: ServiceType
    label_key: str
    icon: str
    fields: tuple[FieldSpec, ...]
    title_field: str
    production_ready: bool = False
    large_job: bool = False


def _text(name: str, *, minimum: int = 2, maximum: int = 500, optional: bool = False) -> FieldSpec:
    return FieldSpec(
        name, f"service.field.{name}", minimum=minimum, maximum=maximum, optional=optional
    )


def _choice(name: str, options: tuple[str, ...]) -> FieldSpec:
    return FieldSpec(name, f"service.field.{name}", FieldKind.CHOICE, options=options)


_LANGUAGE = _choice("language", ("uz", "ru", "en"))
_LENGTH = _choice("length", ("short", "medium", "long"))
_REQUIREMENTS = _text("requirements", maximum=1000, optional=True)

_SERVICE_CATALOG = {
    ServiceType.ESSAY: ServiceDefinition(
        ServiceType.ESSAY,
        "service.essay",
        "📚",
        (_text("topic"), _LANGUAGE, _LENGTH, _REQUIREMENTS),
        "topic",
        production_ready=True,
    ),
    ServiceType.INDEPENDENT_WORK: ServiceDefinition(
        ServiceType.INDEPENDENT_WORK,
        "service.independent",
        "📝",
        (_text("subject"), _text("topic"), _LENGTH, _LANGUAGE, _REQUIREMENTS),
        "topic",
        production_ready=True,
    ),
    ServiceType.COURSE_WORK: ServiceDefinition(
        ServiceType.COURSE_WORK,
        "service.course",
        "📚",
        (
            _text("subject"),
            _text("topic"),
            _text("sections", maximum=1500),
            _LENGTH,
            _LANGUAGE,
            _REQUIREMENTS,
        ),
        "topic",
        production_ready=True,
        large_job=True,
    ),
    ServiceType.SUMMARY: ServiceDefinition(
        ServiceType.SUMMARY,
        "service.summary",
        "📋",
        (
            _text("source", maximum=4000),
            _choice("detail", ("short", "medium", "detailed")),
            _LANGUAGE,
        ),
        "source",
    ),
    ServiceType.QUIZ: ServiceDefinition(
        ServiceType.QUIZ,
        "service.quiz",
        "❓",
        (
            _text("topic"),
            FieldSpec("question_count", "service.field.question_count", FieldKind.INTEGER, 1, 100),
            _choice("difficulty", ("easy", "medium", "hard")),
            _choice("option_count", ("2", "3", "4", "5")),
            FieldSpec(
                "answer_key",
                "service.field.answer_key",
                FieldKind.BOOLEAN,
                options=("yes", "no"),
            ),
            _LANGUAGE,
        ),
        "topic",
    ),
    ServiceType.WRITE_TEXT: ServiceDefinition(
        ServiceType.WRITE_TEXT,
        "service.write",
        "✍️",
        (
            _text("topic"),
            _choice("text_type", ("article", "post", "description", "letter")),
            _choice("style", ("simple", "formal", "academic", "creative")),
            _LENGTH,
            _LANGUAGE,
        ),
        "topic",
    ),
    ServiceType.REWRITE: ServiceDefinition(
        ServiceType.REWRITE,
        "service.rewrite",
        "🔄",
        (
            _text("text", maximum=4000),
            _choice("mode", ("simple", "formal", "academic", "shorten", "clarify")),
        ),
        "text",
    ),
    ServiceType.TRANSLATE: ServiceDefinition(
        ServiceType.TRANSLATE,
        "service.translate",
        "🌐",
        (_text("text", maximum=4000), _choice("target_language", ("uz", "ru", "en"))),
        "text",
    ),
    ServiceType.DOCUMENT: ServiceDefinition(
        ServiceType.DOCUMENT,
        "service.document",
        "📑",
        (
            _choice(
                "document_type",
                ("application", "explanation", "autobiography", "official_letter", "other"),
            ),
            _text("details", maximum=2000),
        ),
        "document_type",
    ),
}
SERVICE_CATALOG = MappingProxyType(_SERVICE_CATALOG)


@dataclass(frozen=True, slots=True)
class PricingCategory:
    code: str
    label_key: str
    price_source: str
    price_uzs: int | None = None


PRICING_CATEGORIES = (
    PricingCategory("presentation", "pricing.presentation", "database_plans"),
    PricingCategory("written_work", "pricing.written_work", "not_configured"),
    PricingCategory("ai_text", "pricing.ai_text", "not_configured"),
    PricingCategory("documents", "pricing.documents", "not_configured"),
)


def validate_service_request(
    service_type: ServiceType | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize only catalog-authorized request fields."""
    canonical_type = ServiceType(service_type)
    definition = SERVICE_CATALOG[canonical_type]
    allowed = {field.name for field in definition.fields}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    normalized: dict[str, Any] = {}
    for field in definition.fields:
        if field.name not in payload:
            if field.optional:
                normalized[field.name] = ""
                continue
            raise ValueError(f"{field.name} is required")
        normalized[field.name] = _normalize_field(field, payload[field.name])
    return normalized


def validate_field_value(
    service_type: ServiceType | str,
    field_name: str,
    raw: Any,
) -> Any:
    definition = SERVICE_CATALOG[ServiceType(service_type)]
    field = next((item for item in definition.fields if item.name == field_name), None)
    if field is None:
        raise ValueError("unknown service field")
    return _normalize_field(field, raw)


def _normalize_field(field: FieldSpec, raw: Any) -> Any:
    if field.kind is FieldKind.INTEGER:
        try:
            integer_value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.name} must be a number") from exc
        if not field.minimum <= integer_value <= field.maximum:
            raise ValueError(f"{field.name} must be between {field.minimum} and {field.maximum}")
        return integer_value
    text_value = str(raw).strip()
    if field.kind is FieldKind.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        text_value = text_value.lower()
        if text_value not in field.options:
            raise ValueError(f"{field.name} has an invalid value")
        return text_value == "yes"
    if field.kind is FieldKind.CHOICE:
        if text_value not in field.options:
            raise ValueError(f"{field.name} has an invalid value")
        return int(text_value) if field.name == "option_count" else text_value
    if not field.optional and not text_value:
        raise ValueError(f"{field.name} is required")
    if text_value and not field.minimum <= len(text_value) <= field.maximum:
        raise ValueError(f"{field.name} length is invalid")
    return text_value
