"""AI provider protocol."""

from typing import Protocol

from modules.ai.contracts import (
    ContentGenerationRequest,
    GeneratedContent,
    Outline,
    PresentationRequest,
)
from modules.ai.essay_contracts import EssayDocument, EssayGenerationRequest


class AIProvider(Protocol):
    async def generate_outline(self, request: PresentationRequest) -> Outline: ...

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent: ...

    async def generate_essay(self, request: EssayGenerationRequest) -> EssayDocument: ...
