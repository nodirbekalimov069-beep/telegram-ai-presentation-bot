import json
import logging

import httpx
import pytest
import respx
from openai import BadRequestError

from modules.ai.contracts import PresentationRequest
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.asyncio
@respx.mock
async def test_outline_retries_transient_provider_errors_then_succeeds() -> None:
    outline = {
        "title": "AI asoslari",
        "slides": [
            {
                "number": 1,
                "title": "Kirish",
                "purpose": "Mavzuni tanishtirish",
                "key_points": ["Asosiy tushuncha"],
                "visual_type": "title",
                "layout": "cover",
                "sources": [],
            }
        ],
    }
    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(408, json={"error": "request timeout"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(502, json={"error": "upstream unavailable"}),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(outline)}}]},
            ),
        ]
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=3,
        retry_base_delay_seconds=0,
    )

    result = await provider.generate_outline(
        PresentationRequest(
            topic="AI asoslari",
            language="uz",
            direction="Ta'lim",
            purpose="Tushuntirish",
            audience="Talabalar",
            knowledge_level="beginner",
            slide_count=1,
            design_style="modern",
        )
    )

    assert result.title == "AI asoslari"
    assert route.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_outline_does_not_retry_permanent_client_error() -> None:
    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": "invalid request"})
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=3,
        retry_base_delay_seconds=0,
    )

    with pytest.raises(BadRequestError):
        await provider.generate_outline(
            PresentationRequest(
                topic="AI asoslari",
                language="uz",
                direction="Ta'lim",
                purpose="Tushuntirish",
                audience="Talabalar",
                knowledge_level="beginner",
                slide_count=1,
                design_style="modern",
            )
        )

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_outline_requests_strict_openai_structured_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outline = {
        "title": "AI asoslari",
        "slides": [
            {
                "number": 1,
                "title": "Kirish",
                "purpose": "Mavzuni tanishtirish",
                "key_points": ["Asosiy tushuncha"],
                "visual_type": "title",
                "layout": "cover",
                "sources": [],
            }
        ],
    }
    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(outline),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
        )
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )

    with caplog.at_level(logging.INFO, logger="modules.ai.providers.openai_compatible"):
        result = await provider.generate_outline(
            PresentationRequest(
                topic="AI asoslari",
                language="uz",
                direction="Ta'lim",
                purpose="Tushuntirish",
                audience="Talabalar",
                knowledge_level="beginner",
                slide_count=1,
                design_style="modern",
            )
        )

    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["name"] == "Outline"
    strict_schema = sent["response_format"]["json_schema"]["schema"]
    serialized_schema = json.dumps(strict_schema)
    assert '"oneOf"' not in serialized_schema
    assert '"discriminator"' not in serialized_schema
    assert '"anyOf"' in serialized_schema
    assert result.title == "AI asoslari"
    assert "schema=Outline" in caplog.text
    assert "system_prompt_sha256=" in caplog.text
    assert "prompt_tokens=100" in caplog.text
    assert "completion_tokens=50" in caplog.text
    assert "total_tokens=150" in caplog.text
    assert "test-key" not in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_outline_recovers_from_one_malformed_structured_response() -> None:
    valid_outline = {
        "title": "AI asoslari",
        "slides": [
            {
                "number": 1,
                "title": "Kirish",
                "purpose": "Mavzuni tanishtirish",
                "key_points": ["Asosiy tushuncha"],
                "visual_type": "title",
                "layout": "cover",
                "sources": [],
            }
        ],
    }

    def response(content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "refusal": None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
        )

    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        side_effect=[response("{}"), response(json.dumps(valid_outline))]
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=1,
        retry_base_delay_seconds=0,
    )

    result = await provider.generate_outline(
        PresentationRequest(
            topic="AI asoslari",
            language="uz",
            direction="Ta'lim",
            purpose="Tushuntirish",
            audience="Talabalar",
            knowledge_level="beginner",
            slide_count=1,
            design_style="modern",
        )
    )

    second_request = json.loads(route.calls[1].request.content)
    assert result.title == "AI asoslari"
    assert route.call_count == 2
    assert "schema validation" in second_request["messages"][-1]["content"]


@pytest.mark.asyncio
@respx.mock
async def test_outline_recovers_from_valid_schema_with_wrong_slide_count() -> None:
    def outline(slide_count: int) -> dict[str, object]:
        return {
            "title": "AI asoslari",
            "slides": [
                {
                    "number": number,
                    "title": f"Slayd {number}",
                    "purpose": "Mavzuni tushuntirish",
                    "key_points": [f"Asosiy fikr {number}"],
                    "visual_type": "content",
                    "layout": "standard",
                    "sources": [],
                }
                for number in range(1, slide_count + 1)
            ],
        }

    def response(payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-count",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(payload),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
        )

    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        side_effect=[response(outline(1)), response(outline(2))]
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=1,
        retry_base_delay_seconds=0,
    )

    result = await provider.generate_outline(
        PresentationRequest(
            topic="AI asoslari",
            language="uz",
            direction="Ta'lim",
            purpose="Tushuntirish",
            audience="Talabalar",
            knowledge_level="beginner",
            slide_count=2,
            design_style="modern",
        )
    )

    assert len(result.slides) == 2
    assert route.call_count == 2
