import json

import pytest
import respx
from httpx import Response
from modules.ai.presentation_agent import AgentBackendContext
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.asyncio
@respx.mock
async def test_provider_runs_presentation_agent_with_trusted_backend_context() -> None:
    route = respx.post("https://ai.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "reply_text": "Nechta slayd va kimlar uchun?",
                                    "language": "uz",
                                    "stage": "collecting",
                                    "collected_data": {"topic": "AI"},
                                    "missing_fields": ["slide_count", "audience"],
                                    "outline": None,
                                    "outline_approved": False,
                                }
                            )
                        }
                    }
                ]
            },
        )
    )
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example/v1",
        api_key="secret",
        model="model",
    )
    backend = AgentBackendContext(
        telegram_user_id=7,
        free_demo_available=False,
        available_formats=["pptx", "pdf"],
    )

    user_text = "AI haqida slayd USER-UNIQUE-42"
    reply = await provider.respond_to_user(
        user_text=user_text,
        backend=backend,
        conversation_data={},
    )

    assert reply.stage == "collecting"
    assert reply.missing_fields == ["slide_count", "audience"]
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"] == {"type": "json_object"}
    assert len(sent["messages"]) == 3
    assert user_text not in sent["messages"][0]["content"]
    assert json.loads(sent["messages"][1]["content"])["free_demo_available"] is False
