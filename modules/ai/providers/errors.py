"""Provider-neutral AI failure contracts."""

from enum import StrEnum


class QuizRejectionCategory(StrEnum):
    """Bounded, content-free reasons for rejecting a QUIZ provider response."""

    SCHEMA_INVALID = "schema_invalid"
    DUPLICATE = "duplicate"
    SEMANTIC_INVALID = "semantic_invalid"
    OTHER_INVALID = "other_invalid"


class ProviderCallBudgetExhausted(RuntimeError):
    """A durable logical-generation outbound-call budget is exhausted."""
