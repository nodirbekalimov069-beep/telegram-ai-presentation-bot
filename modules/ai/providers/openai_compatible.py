"""Official async OpenAI SDK adapter with strict structured responses."""

import asyncio
import hashlib
import logging
import math
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from typing import TypeVar, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from modules.ai.contracts import (
    AIContractError,
    ContentGenerationRequest,
    GeneratedContent,
    Outline,
    PresentationRequest,
    build_content_messages,
    build_openai_messages,
    parse_outline,
    validate_generated_content,
)
from modules.ai.course_work_contracts import (
    CourseWorkDocument,
    CourseWorkGenerationRequest,
    build_course_work_messages,
    materialize_and_validate_course_work,
)
from modules.ai.document_contracts import (
    DocumentGenerationRequest,
    DocumentSelection,
    PreparedDocument,
    build_document_messages,
    materialize_document_selection,
    validate_document_selection,
)
from modules.ai.essay_contracts import (
    EssayDocument,
    EssayGenerationRequest,
    build_essay_messages,
    validate_essay_against_request,
)
from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
    build_independent_work_messages,
    materialize_and_validate_independent_work,
)
from modules.ai.quiz_contracts import (
    QuizDocument,
    QuizGenerationRequest,
    build_quiz_messages,
    build_quiz_retry_message,
    classify_quiz_semantic_reason,
    validate_quiz_against_request,
)
from modules.ai.rewrite_contracts import (
    RewriteDocument,
    RewriteGenerationRequest,
    RewriteSelection,
    build_rewrite_messages,
    materialize_and_validate_rewrite,
    validate_rewrite_selection,
)
from modules.ai.semantic_risk import SemanticRiskBoundary, enforce_semantic_risk
from modules.ai.summary_contracts import (
    SummaryDocument,
    SummaryGenerationRequest,
    SummarySelection,
    build_summary_messages,
    materialize_summary_selection,
    validate_summary_selection,
)
from modules.ai.translation_contracts import (
    TranslationDocument,
    TranslationGenerationRequest,
    TranslationSelection,
    build_translation_messages,
    materialize_and_validate_translation,
    source_translation_segments,
    translation_text_is_language_neutral,
    validate_translation_selection,
)
from modules.ai.write_text_contracts import (
    WriteTextDocument,
    WriteTextGenerationRequest,
    build_write_text_messages,
    build_write_text_retry_message,
    validate_write_text_against_request,
)
from modules.ai.presentation_agent import (
    AgentBackendContext,
    AgentReply,
    build_agent_messages,
)
from modules.ai.providers.errors import (
    ProviderCallBudgetExhausted,
    QuizRejectionCategory,
    QuizSemanticReason,
)

StructuredResponse = TypeVar("StructuredResponse", bound=BaseModel)
_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
_CONTENT_BATCH_SIZE = 5
_MAX_COMPLETION_TOKENS = 8192
logger = logging.getLogger(__name__)


def _classify_quiz_rejection(
    error: Exception, *, schema_error: bool | None
) -> QuizRejectionCategory:
    """Classify without exposing rejected values or free-form provider content."""
    if schema_error is None:
        return QuizRejectionCategory.OTHER_INVALID
    summary = str(error).casefold()
    if "duplicate" in summary or "unique" in summary or "questions ask the same fact" in summary:
        return QuizRejectionCategory.DUPLICATE
    if schema_error:
        return QuizRejectionCategory.SCHEMA_INVALID
    return QuizRejectionCategory.SEMANTIC_INVALID


_QUIZ_SEMANTIC_FEEDBACK = {
    QuizSemanticReason.INVALID_CORRECT_ANSWER: (
        "Invalid correct answer: make correct_answer exactly equal one and only one emitted option."
    ),
    QuizSemanticReason.AMBIGUOUS_CORRECT_ANSWER: (
        "Ambiguous correct answer: make exactly one option defensibly correct and remove all/none "
        "or overlapping alternatives."
    ),
    QuizSemanticReason.INVALID_OPTIONS: (
        "Invalid options: emit exactly option_count non-empty, plausible, balanced options for "
        "every question."
    ),
    QuizSemanticReason.DUPLICATE_OPTIONS: (
        "Duplicate options: make every option unique after Unicode and whitespace normalization."
    ),
    QuizSemanticReason.DUPLICATE_QUESTION: (
        "Duplicate question: make every question unique and materially different."
    ),
    QuizSemanticReason.QUESTION_ANSWER_MISMATCH: (
        "Question answer mismatch: make the premises determine the keyed answer without revealing "
        "it or disclosing another question's answer."
    ),
    QuizSemanticReason.INVALID_EXPLANATION: (
        "Invalid explanation: explain positively why the keyed answer is correct without supporting, "
        "repeating, or contradicting a distractor."
    ),
    QuizSemanticReason.LANGUAGE_MISMATCH: (
        "Language mismatch: rewrite every authored block in the requested language using natural, "
        "diverse vocabulary without mixed-language filler."
    ),
    QuizSemanticReason.MALFORMED_CONTENT: (
        "Malformed content: rebuild the authored content to satisfy source-free safety, application "
        "scenario, requirement, and prompt-leakage constraints."
    ),
    QuizSemanticReason.OTHER_SEMANTIC: (
        "Other semantic: rebuild the quiz and satisfy every request-specific semantic invariant."
    ),
}


def _quiz_corrective_feedback(
    category: QuizRejectionCategory,
    *,
    semantic_reason: QuizSemanticReason | None = None,
) -> str:
    """Return bounded category-only feedback; never include rejected content."""
    if (
        category is QuizRejectionCategory.DUPLICATE
        and semantic_reason is QuizSemanticReason.DUPLICATE_QUESTION
    ):
        return (
            "The previous response repeated a question. Make every question unique. Return one "
            "complete JSON object only, with no text outside the structured output."
        )
    if semantic_reason is not None and (
        category is QuizRejectionCategory.SEMANTIC_INVALID
        or semantic_reason is not QuizSemanticReason.OTHER_SEMANTIC
    ):
        rule = _QUIZ_SEMANTIC_FEEDBACK[semantic_reason]
        return (
            f"The previous response was {category.value.replace('_', ' ')}. Reason: "
            f"{semantic_reason.value.replace('_', ' ')}. {rule} Do not repeat any previous "
            "question. Return one complete JSON object only, matching the required schema and "
            "every requested constraint."
        )
    return {
        QuizRejectionCategory.SCHEMA_INVALID: (
            "The previous response was schema invalid. Return one complete JSON object "
            "that exactly matches the required schema and field types."
        ),
        QuizRejectionCategory.DUPLICATE: build_quiz_retry_message("quiz options must be unique"),
        QuizRejectionCategory.SEMANTIC_INVALID: (
            "The previous response was semantic invalid. Correct every request-specific, "
            "language, factual-safety, and consistency constraint without weakening validation."
        ),
        QuizRejectionCategory.OTHER_INVALID: (
            "The previous response was other invalid. Return one complete, non-refusal JSON "
            "object satisfying the schema and every requested constraint."
        ),
    }[category]


class _StructuredResponseBudgetExhausted(AIContractError):
    """The bounded structured-response attempt budget is exhausted."""


_UZBEK_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "Ў": "O‘",
        "ў": "o‘",
        "Қ": "Q",
        "қ": "q",
        "Ғ": "G‘",
        "ғ": "g‘",
        "Ҳ": "H",
        "ҳ": "h",
        "Ё": "Yo",
        "ё": "yo",
        "Ж": "J",
        "ж": "j",
        "Ц": "S",
        "ц": "s",
        "Ч": "Ch",
        "ч": "ch",
        "Ш": "Sh",
        "ш": "sh",
        "Щ": "Sh",
        "щ": "sh",
        "Ю": "Yu",
        "ю": "yu",
        "Я": "Ya",
        "я": "ya",
        "А": "A",
        "а": "a",
        "Б": "B",
        "б": "b",
        "В": "V",
        "в": "v",
        "Г": "G",
        "г": "g",
        "Д": "D",
        "д": "d",
        "Е": "E",
        "е": "e",
        "З": "Z",
        "з": "z",
        "И": "I",
        "и": "i",
        "Й": "Y",
        "й": "y",
        "К": "K",
        "к": "k",
        "Л": "L",
        "л": "l",
        "М": "M",
        "м": "m",
        "Н": "N",
        "н": "n",
        "О": "O",
        "о": "o",
        "П": "P",
        "п": "p",
        "Р": "R",
        "р": "r",
        "С": "S",
        "с": "s",
        "Т": "T",
        "т": "t",
        "У": "U",
        "у": "u",
        "Ф": "F",
        "ф": "f",
        "Х": "X",
        "х": "x",
        "Ъ": "",
        "ъ": "",
        "Ь": "",
        "ь": "",
        "Э": "E",
        "э": "e",
    }
)


def _semantic_risk_source(text: str, source_language: str | None) -> str:
    """Normalize Uzbek Cyrillic for the pinned NLI model, not for app invariants."""
    if source_language == "uz" and any("CYRILLIC" in unicodedata.name(c, "") for c in text):
        return text.translate(_UZBEK_CYRILLIC_TO_LATIN)
    return text


def _validation_error_summary(error: ValidationError) -> str:
    """Return bounded schema diagnostics without echoing rejected model values."""
    summaries: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        summaries.append(f"{location or '<root>'}: {item['msg']} ({item['type']})")
    return "; ".join(summaries)[:1000]


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        max_retries: int = 3,
        retry_base_delay_seconds: float = 0.5,
        semantic_risk_boundary: SemanticRiskBoundary | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AI API key is required")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError("AI timeout must be finite and within 0-300 seconds")
        if not 0 <= max_retries <= 10:
            raise ValueError("AI max_retries must be within 0-10")
        if not math.isfinite(retry_base_delay_seconds) or not 0 <= retry_base_delay_seconds <= 10:
            raise ValueError("AI retry delay must be finite and within 0-10 seconds")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._semantic_risk_boundary = semantic_risk_boundary
        self._quiz_call_reserver: Callable[[], Awaitable[None]] | None = None
        self._quiz_rejection_recorder: Callable[[QuizRejectionCategory], Awaitable[None]] | None = (
            None
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def aclose(self) -> None:
        await self._client.close()

    def set_quiz_call_reserver(self, reserver: Callable[[], Awaitable[None]]) -> None:
        self._quiz_call_reserver = reserver

    def set_quiz_rejection_recorder(
        self, recorder: Callable[[QuizRejectionCategory], Awaitable[None]]
    ) -> None:
        self._quiz_rejection_recorder = recorder

    async def _record_quiz_rejection(
        self, category: QuizRejectionCategory, *, attempt: int
    ) -> None:
        logger.info(
            "quiz structured response rejected category=%s attempt=%d",
            category.value,
            attempt,
        )
        if self._quiz_rejection_recorder is not None:
            await self._quiz_rejection_recorder(category)

    async def _request_structured(
        self,
        *,
        messages: Iterable[ChatCompletionMessageParam],
        response_format: type[StructuredResponse],
        response_validator: Callable[[StructuredResponse], StructuredResponse] | None = None,
        contract_retry_builder: Callable[[str], str] | None = None,
        retry_invalid_response: bool = True,
        max_attempts: int | None = None,
        before_outbound: Callable[[], Awaitable[None]] | None = None,
        temperature: float = 0.2,
    ) -> StructuredResponse:
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("AI temperature must be finite and within 0-2")
        request_messages = list(messages)
        system_content = request_messages[0].get("content") if request_messages else None
        system_prompt_sha256 = (
            hashlib.sha256(system_content.encode("utf-8")).hexdigest()
            if isinstance(system_content, str)
            else "unavailable"
        )
        logger.info(
            "openai structured request model=%s schema=%s messages=%d system_prompt_sha256=%s",
            self._model,
            response_format.__name__,
            len(request_messages),
            system_prompt_sha256,
        )
        attempt_limit = self._max_retries + 1 if max_attempts is None else max_attempts
        for attempt in range(attempt_limit):
            try:
                if before_outbound is not None:
                    await before_outbound()
                completion = await self._client.chat.completions.parse(
                    model=self._model,
                    messages=request_messages,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=_MAX_COMPLETION_TOKENS,
                )
            except _TRANSIENT_ERRORS:
                if attempt + 1 >= attempt_limit:
                    raise
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            except APIStatusError as exc:
                transient_status = exc.status_code in {408, 409} or exc.status_code >= 500
                if not transient_status or attempt + 1 >= attempt_limit:
                    raise
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            except ValidationError as exc:
                if not retry_invalid_response:
                    raise AIContractError(
                        f"AI provider returned invalid {response_format.__name__}: "
                        f"{_validation_error_summary(exc)}"
                    ) from exc
                category = QuizRejectionCategory.SCHEMA_INVALID
                semantic_reason: QuizSemanticReason | None = None
                if response_format is QuizDocument:
                    category = _classify_quiz_rejection(exc, schema_error=True)
                    semantic_reason = classify_quiz_semantic_reason(exc)
                    await self._record_quiz_rejection(category, attempt=attempt + 1)
                if attempt + 1 >= attempt_limit:
                    raise _StructuredResponseBudgetExhausted(
                        "AI provider repeatedly returned an invalid structured response"
                    ) from exc
                if response_format is QuizDocument:
                    correction_message = _quiz_corrective_feedback(
                        category,
                        semantic_reason=semantic_reason,
                    )
                else:
                    correction_message = (
                        "The previous response failed schema validation. "
                        "Return one corrected JSON object matching the required schema. "
                        f"Schema failures: {_validation_error_summary(exc)}"
                    )
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": "system",
                            "content": correction_message,
                        },
                    )
                )
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            usage = completion.usage
            logger.info(
                "openai structured response model=%s schema=%s prompt_tokens=%s "
                "completion_tokens=%s total_tokens=%s",
                self._model,
                response_format.__name__,
                usage.prompt_tokens if usage else None,
                usage.completion_tokens if usage else None,
                usage.total_tokens if usage else None,
            )
            message = completion.choices[0].message
            if message.refusal:
                if response_format is not QuizDocument or not retry_invalid_response:
                    raise AIContractError("AI provider refused the structured request")
                category = QuizRejectionCategory.OTHER_INVALID
                await self._record_quiz_rejection(category, attempt=attempt + 1)
                if attempt + 1 >= attempt_limit:
                    raise _StructuredResponseBudgetExhausted(
                        "AI provider repeatedly refused the structured request"
                    )
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {"role": "system", "content": _quiz_corrective_feedback(category)},
                    )
                )
                continue
            if message.parsed is None:
                if not retry_invalid_response:
                    raise AIContractError("AI provider returned no structured response")
                category = QuizRejectionCategory.SCHEMA_INVALID
                if response_format is QuizDocument:
                    await self._record_quiz_rejection(category, attempt=attempt + 1)
                if attempt + 1 >= attempt_limit:
                    raise _StructuredResponseBudgetExhausted(
                        "AI provider repeatedly returned no structured response"
                    )
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": "system",
                            "content": (
                                _quiz_corrective_feedback(category)
                                if response_format is QuizDocument
                                else "The previous response contained no valid structured object. "
                                "Return one corrected JSON object matching the required schema."
                            ),
                        },
                    )
                )
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            if response_validator is not None:
                try:
                    return response_validator(message.parsed)
                except AIContractError as exc:
                    if not retry_invalid_response:
                        raise
                    category = QuizRejectionCategory.SEMANTIC_INVALID
                    semantic_reason = classify_quiz_semantic_reason(exc)
                    if response_format is QuizDocument:
                        category = _classify_quiz_rejection(exc, schema_error=False)
                        await self._record_quiz_rejection(category, attempt=attempt + 1)
                    if attempt + 1 >= attempt_limit:
                        raise _StructuredResponseBudgetExhausted(
                            "AI provider repeatedly returned a response violating the request contract"
                        ) from exc
                    if contract_retry_builder is not None:
                        correction = contract_retry_builder(str(exc))
                    elif response_format is QuizDocument:
                        correction = _quiz_corrective_feedback(
                            category,
                            semantic_reason=semantic_reason,
                        )
                    elif response_format.__name__ == WriteTextDocument.__name__:
                        correction = build_write_text_retry_message(str(exc))
                    else:
                        correction = (
                            "The previous response failed schema validation or request-specific "
                            "contract validation. Return one corrected JSON object matching every "
                            "requested constraint."
                        )
                    request_messages.append(
                        cast(
                            ChatCompletionMessageParam,
                            {
                                "role": "system",
                                "content": correction,
                            },
                        )
                    )
                    await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                    continue
            return message.parsed
        raise RuntimeError("AI structured request retry loop exited unexpectedly")

    async def generate_outline(self, request: PresentationRequest) -> Outline:
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_openai_messages(request),
            ),
            response_format=Outline,
            response_validator=lambda outline: parse_outline(
                outline.model_dump_json(),
                expected_slide_count=request.slide_count,
            ),
        )

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent:
        raw_slides = request.outline.get("slides", [])
        if not isinstance(raw_slides, list) or not raw_slides:
            raise AIContractError("content generation requires at least one outline slide")
        generated_slides = []
        for start in range(0, len(raw_slides), _CONTENT_BATCH_SIZE):
            batch = raw_slides[start : start + _CONTENT_BATCH_SIZE]
            numbers = [int(slide.get("number", 0)) for slide in batch if isinstance(slide, dict)]
            batch_outline = {
                **request.outline,
                "slides": batch,
                "deck_slide_count": len(raw_slides),
                "batch_range": [start + 1, start + len(batch)],
            }
            batch_request = request.model_copy(update={"outline": batch_outline})

            validate_batch = partial(
                validate_generated_content,
                expected_slide_count=len(numbers),
                language=request.language,
                expected_slide_numbers=numbers,
                enforce_repetition=False,
            )

            result = await self._request_structured(
                messages=cast(
                    Iterable[ChatCompletionMessageParam],
                    build_content_messages(batch_request),
                ),
                response_format=GeneratedContent,
                response_validator=validate_batch,
            )
            generated_slides.extend(result.slides)
        combined = GeneratedContent(title=request.title, slides=generated_slides)
        return validate_generated_content(
            combined,
            expected_slide_count=len(raw_slides),
            language=request.language,
        )

    async def generate_essay(self, request: EssayGenerationRequest) -> EssayDocument:
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_essay_messages(request),
            ),
            response_format=EssayDocument,
            response_validator=partial(validate_essay_against_request, request=request),
        )

    async def generate_independent_work(
        self, request: IndependentWorkGenerationRequest
    ) -> IndependentWorkDocument:
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_independent_work_messages(request),
            ),
            response_format=IndependentWorkDocument,
            response_validator=partial(materialize_and_validate_independent_work, request=request),
        )

    async def generate_course_work(
        self, request: CourseWorkGenerationRequest
    ) -> CourseWorkDocument:
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_course_work_messages(request),
            ),
            response_format=CourseWorkDocument,
            response_validator=partial(materialize_and_validate_course_work, request=request),
        )

    async def generate_summary(self, request: SummaryGenerationRequest) -> SummaryDocument:
        selection = await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_summary_messages(request),
            ),
            response_format=SummarySelection,
            response_validator=partial(validate_summary_selection, request=request),
        )
        return materialize_summary_selection(selection, request=request)

    async def generate_quiz(self, request: QuizGenerationRequest) -> QuizDocument:
        try:
            return await self._request_structured(
                messages=cast(
                    Iterable[ChatCompletionMessageParam],
                    build_quiz_messages(request),
                ),
                response_format=QuizDocument,
                response_validator=partial(validate_quiz_against_request, request=request),
                max_attempts=4,
                before_outbound=self._quiz_call_reserver,
            )
        except _TRANSIENT_ERRORS as exc:
            raise ProviderCallBudgetExhausted(
                "quiz provider outbound-call budget exhausted"
            ) from exc
        except APIStatusError as exc:
            if exc.status_code not in {408, 409} and exc.status_code < 500:
                raise
            raise ProviderCallBudgetExhausted(
                "quiz provider outbound-call budget exhausted"
            ) from exc
        except _StructuredResponseBudgetExhausted as exc:
            raise ProviderCallBudgetExhausted(
                "quiz provider structured-response budget exhausted"
            ) from exc

    async def generate_write_text(self, request: WriteTextGenerationRequest) -> WriteTextDocument:
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_write_text_messages(request),
            ),
            response_format=WriteTextDocument,
            response_validator=partial(validate_write_text_against_request, request=request),
            contract_retry_builder=partial(build_write_text_retry_message, request=request),
        )

    async def generate_rewrite(self, request: RewriteGenerationRequest) -> RewriteDocument:
        selection = await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_rewrite_messages(request),
            ),
            response_format=RewriteSelection,
            response_validator=partial(validate_rewrite_selection, request=request),
            retry_invalid_response=False,
            temperature=0.0,
        )
        return materialize_and_validate_rewrite(selection, request=request)

    async def generate_translation(
        self, request: TranslationGenerationRequest
    ) -> TranslationDocument:
        # The provider generates candidate text only. Its graphs, verdicts or
        # confidence claims are intentionally outside the acceptance boundary.
        if self._semantic_risk_boundary is None:
            raise AIContractError("independent semantic risk boundary is unavailable")
        messages = cast(Iterable[ChatCompletionMessageParam], build_translation_messages(request))
        last_risk_error: AIContractError | None = None
        for risk_attempt in range(2):
            selection = await self._request_structured(
                messages=messages,
                response_format=TranslationSelection,
                response_validator=partial(validate_translation_selection, request=request),
                retry_invalid_response=True,
                temperature=0.0,
            )
            source_segments = source_translation_segments(request.text)
            try:
                for (_, source_text), candidate in zip(
                    source_segments, selection.segments, strict=True
                ):
                    if source_text == candidate.text:
                        if translation_text_is_language_neutral(source_text):
                            continue
                        raise AIContractError("non-neutral translation candidate was unchanged")
                    try:
                        normalized_source = _semantic_risk_source(
                            source_text, request.source_language
                        )
                        risk = await self._semantic_risk_boundary.assess(
                            normalized_source,
                            candidate.text,
                        )
                        reverse_risk = await self._semantic_risk_boundary.assess(
                            candidate.text,
                            normalized_source,
                        )
                    except AIContractError:
                        raise
                    except Exception as exc:
                        raise AIContractError(
                            "independent semantic risk boundary is unavailable"
                        ) from exc
                    enforce_semantic_risk(risk)
                    enforce_semantic_risk(reverse_risk)
            except AIContractError as exc:
                last_risk_error = exc
                if risk_attempt == 0:
                    continue
                raise AIContractError(
                    "translation repeatedly failed independent semantic risk validation"
                ) from exc
            return materialize_and_validate_translation(selection, request=request)
        raise AIContractError("translation semantic risk validation failed") from last_risk_error

    async def generate_document(self, request: DocumentGenerationRequest) -> PreparedDocument:
        selection = await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_document_messages(request),
            ),
            response_format=DocumentSelection,
            response_validator=partial(validate_document_selection, request=request),
            temperature=0.0,
        )
        return materialize_document_selection(selection, request=request)

    async def _request_json_object(
        self,
        *,
        messages: Iterable[ChatCompletionMessageParam],
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        request_messages = list(messages)
        for attempt in range(self._max_retries + 1):
            try:
                completion = await self._client.chat.completions.create(
                    model=self._model,
                    messages=request_messages,
                    response_format={"type": "json_object"},
                    temperature=0.2,
                    max_tokens=_MAX_COMPLETION_TOKENS,
                )
            except _TRANSIENT_ERRORS:
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            except APIStatusError as exc:
                transient_status = exc.status_code in {408, 409} or exc.status_code >= 500
                if not transient_status or attempt >= self._max_retries:
                    raise
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            message = completion.choices[0].message
            if message.refusal:
                raise AIContractError("AI provider refused the JSON request")
            try:
                parsed = response_model.model_validate_json(message.content or "")
            except ValidationError as exc:
                if attempt >= self._max_retries:
                    raise AIContractError(
                        "AI provider repeatedly returned an invalid JSON response"
                    ) from exc
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": "system",
                            "content": (
                                "The previous response failed contract validation. "
                                "Return one corrected JSON object only."
                            ),
                        },
                    )
                )
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue
            usage = completion.usage
            logger.info(
                "openai JSON response model=%s schema=%s prompt_tokens=%s "
                "completion_tokens=%s total_tokens=%s",
                self._model,
                response_model.__name__,
                usage.prompt_tokens if usage else None,
                usage.completion_tokens if usage else None,
                usage.total_tokens if usage else None,
            )
            return parsed
        raise RuntimeError("AI JSON request retry loop exited unexpectedly")

    async def respond_to_user(
        self,
        *,
        user_text: str,
        backend: AgentBackendContext,
        conversation_data: dict[str, object],
    ) -> AgentReply:
        return await self._request_json_object(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_agent_messages(
                    user_text=user_text,
                    backend=backend,
                    conversation_data=conversation_data,
                ),
            ),
            response_model=AgentReply,
        )
