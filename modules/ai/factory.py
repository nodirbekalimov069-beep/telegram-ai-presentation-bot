"""AI provider dependency factory."""

import inspect

from core.config import Settings
from modules.ai.providers.base import AIProvider
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.semantic_risk import OnnxSemanticRiskBoundary


def build_ai_provider(settings: Settings) -> AIProvider:
    if settings.ai_provider == "mock":
        return MockAIProvider()
    return OpenAICompatibleProvider(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key.get_secret_value(),
        model=settings.ai_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_retries=settings.ai_max_retries,
        retry_base_delay_seconds=settings.ai_retry_base_delay_seconds,
        semantic_risk_boundary=OnnxSemanticRiskBoundary(
            model_dir=settings.semantic_risk_model_dir,
            timeout_seconds=settings.semantic_risk_timeout_seconds,
        ),
    )


async def close_ai_provider(provider: AIProvider) -> None:
    close = getattr(provider, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
