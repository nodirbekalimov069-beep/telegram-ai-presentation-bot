import json

import httpx
import pytest
import respx

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
        max_retries=2,
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
    assert route.call_count == 3


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

    with pytest.raises(httpx.HTTPStatusError):
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
