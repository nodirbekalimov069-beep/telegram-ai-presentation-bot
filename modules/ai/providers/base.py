"""AI provider protocol."""

from typing import Protocol

from modules.ai.contracts import Outline, PresentationRequest


class AIProvider(Protocol):
    async def generate_outline(self, request: PresentationRequest) -> Outline: ...
