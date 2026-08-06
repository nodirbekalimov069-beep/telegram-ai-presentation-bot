"""OpenAI-compatible chat-completions provider adapter."""

import asyncio
from typing import Any

import httpx

from modules.ai.contracts import Outline, PresentationRequest, build_openai_messages, parse_outline
from modules.ai.presentation_agent import (
    AgentBackendContext,
    AgentReply,
    build_agent_messages,
)


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
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)

    async def _post_json(self, payload: dict[str, object]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.post("/chat/completions", json=payload)
                except httpx.RequestError:
                    if attempt >= self._max_retries:
                        raise
                else:
                    transient = response.status_code in {408, 429} or response.status_code >= 500
                    if not transient or attempt >= self._max_retries:
                        response.raise_for_status()
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError("AI provider returned a non-object response")
                        return data
                await asyncio.sleep(self._retry_base_delay_seconds * (2**attempt))
        raise RuntimeError("AI request retry loop exited unexpectedly")

    async def generate_outline(self, request: PresentationRequest) -> Outline:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": build_openai_messages(request),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        data = await self._post_json(payload)
        raw = data["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            raise ValueError("AI provider returned non-text content")
        return parse_outline(raw, expected_slide_count=request.slide_count)

    async def respond_to_user(
        self,
        *,
        user_text: str,
        backend: AgentBackendContext,
        conversation_data: dict[str, object],
    ) -> AgentReply:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": build_agent_messages(
                user_text=user_text,
                backend=backend,
                conversation_data=conversation_data,
            ),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        data = await self._post_json(payload)
        raw = data["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            raise ValueError("AI provider returned non-text content")
        return AgentReply.model_validate_json(raw)
