"""Official async OpenAI SDK adapter with strict structured responses."""

import asyncio
import hashlib
import logging
import math
from collections.abc import Callable, Iterable
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
from modules.ai.summary_contracts import (
    SummaryDocument,
    SummaryGenerationRequest,
    SummarySelection,
    build_summary_messages,
    materialize_summary_selection,
    validate_summary_selection,
)
from modules.ai.presentation_agent import (
    AgentBackendContext,
    AgentReply,
    build_agent_messages,
)

StructuredResponse = TypeVar("StructuredResponse", bound=BaseModel)
_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
_CONTENT_BATCH_SIZE = 5
_MAX_COMPLETION_TOKENS = 8192
logger = logging.getLogger(__name__)


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
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def _request_structured(
        self,
        *,
        messages: Iterable[ChatCompletionMessageParam],
        response_format: type[StructuredResponse],
        response_validator: Callable[[StructuredResponse], StructuredResponse] | None = None,
    ) -> StructuredResponse:
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
        for attempt in range(self._max_retries + 1):
            try:
                completion = await self._client.chat.completions.parse(
                    model=self._model,
                    messages=request_messages,
                    response_format=response_format,
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
            except ValidationError as exc:
                if attempt >= self._max_retries:
                    raise AIContractError(
                        "AI provider repeatedly returned an invalid structured response"
                    ) from exc
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": "system",
                            "content": (
                                "The previous response failed schema validation. "
                                "Return one corrected JSON object matching the required schema."
                            ),
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
                raise AIContractError("AI provider refused the structured request")
            if message.parsed is None:
                if attempt >= self._max_retries:
                    raise AIContractError("AI provider repeatedly returned no structured response")
                request_messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": "system",
                            "content": (
                                "The previous response contained no valid structured object. "
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
                    if attempt >= self._max_retries:
                        raise AIContractError(
                            "AI provider repeatedly returned a response violating the request contract"
                        ) from exc
                    request_messages.append(
                        cast(
                            ChatCompletionMessageParam,
                            {
                                "role": "system",
                                "content": (
                                    "The previous response failed schema validation or "
                                    "request-specific contract validation. Return one corrected "
                                    "JSON object matching every requested constraint. Failure: "
                                    f"{exc}"
                                ),
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
