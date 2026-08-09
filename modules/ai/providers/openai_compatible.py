"""Official async OpenAI SDK adapter with strict structured responses."""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable
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
from modules.ai.presentation_agent import (
    AgentBackendContext,
    AgentReply,
    build_agent_messages,
)

StructuredResponse = TypeVar("StructuredResponse", bound=BaseModel)
_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
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
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_retries = max(0, max_retries)
        self._retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)
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
                raise AIContractError("AI provider returned no structured response")
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
                                    "JSON object matching every requested constraint."
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
        expected_slide_count = len(raw_slides) if isinstance(raw_slides, list) else 0
        return await self._request_structured(
            messages=cast(
                Iterable[ChatCompletionMessageParam],
                build_content_messages(request),
            ),
            response_format=GeneratedContent,
            response_validator=lambda content: validate_generated_content(
                content,
                expected_slide_count=expected_slide_count,
            ),
        )

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
