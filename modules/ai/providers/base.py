"""AI provider protocol."""

from typing import Protocol

from modules.ai.contracts import (
    ContentGenerationRequest,
    GeneratedContent,
    Outline,
    PresentationRequest,
)


class AIProvider(Protocol):
    async def generate_outline(self, request: PresentationRequest) -> Outline: ...

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent: ...
