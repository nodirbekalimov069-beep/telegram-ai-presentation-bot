"""Provider-neutral AI failure contracts."""

from enum import StrEnum


class QuizRejectionCategory(StrEnum):
    """Bounded, content-free reasons for rejecting a QUIZ provider response."""

    SCHEMA_INVALID = "schema_invalid"
    DUPLICATE = "duplicate"
    SEMANTIC_INVALID = "semantic_invalid"
    OTHER_INVALID = "other_invalid"


class QuizSemanticReason(StrEnum):
    """Closed, provider-neutral reasons for finite QUIZ repair guidance."""

    INVALID_CORRECT_ANSWER = "invalid_correct_answer"
    AMBIGUOUS_CORRECT_ANSWER = "ambiguous_correct_answer"
    INVALID_OPTIONS = "invalid_options"
    DUPLICATE_OPTIONS = "duplicate_options"
    DUPLICATE_QUESTION = "duplicate_question"
    QUESTION_ANSWER_MISMATCH = "question_answer_mismatch"
    INVALID_EXPLANATION = "invalid_explanation"
    LANGUAGE_MISMATCH = "language_mismatch"
    MALFORMED_CONTENT = "malformed_content"
    OTHER_SEMANTIC = "other_semantic"


class ProviderCallBudgetExhausted(RuntimeError):
    """A durable logical-generation outbound-call budget is exhausted."""
