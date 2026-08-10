"""AI provider protocol."""

from typing import Protocol

from modules.ai.contracts import (
    ContentGenerationRequest,
    GeneratedContent,
    Outline,
    PresentationRequest,
)
from modules.ai.course_work_contracts import CourseWorkDocument, CourseWorkGenerationRequest
from modules.ai.essay_contracts import EssayDocument, EssayGenerationRequest
from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
)
from modules.ai.summary_contracts import SummaryDocument, SummaryGenerationRequest


class AIProvider(Protocol):
    async def generate_outline(self, request: PresentationRequest) -> Outline: ...

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent: ...

    async def generate_essay(self, request: EssayGenerationRequest) -> EssayDocument: ...

    async def generate_independent_work(
        self, request: IndependentWorkGenerationRequest
    ) -> IndependentWorkDocument: ...

    async def generate_course_work(
        self, request: CourseWorkGenerationRequest
    ) -> CourseWorkDocument: ...

    async def generate_summary(self, request: SummaryGenerationRequest) -> SummaryDocument: ...
