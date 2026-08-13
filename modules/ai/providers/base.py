"""AI provider protocol."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from modules.ai.contracts import (
    ContentGenerationRequest,
    GeneratedContent,
    Outline,
    PresentationRequest,
)
from modules.ai.course_work_contracts import CourseWorkDocument, CourseWorkGenerationRequest
from modules.ai.document_contracts import DocumentGenerationRequest, PreparedDocument
from modules.ai.essay_contracts import EssayDocument, EssayGenerationRequest
from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
)
from modules.ai.quiz_contracts import QuizDocument, QuizGenerationRequest
from modules.ai.rewrite_contracts import RewriteDocument, RewriteGenerationRequest
from modules.ai.summary_contracts import SummaryDocument, SummaryGenerationRequest
from modules.ai.translation_contracts import TranslationDocument, TranslationGenerationRequest
from modules.ai.write_text_contracts import WriteTextDocument, WriteTextGenerationRequest


class AIProvider(Protocol):
    def set_quiz_call_reserver(self, reserver: Callable[[], Awaitable[None]]) -> None: ...

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

    async def generate_quiz(self, request: QuizGenerationRequest) -> QuizDocument: ...

    async def generate_write_text(
        self, request: WriteTextGenerationRequest
    ) -> WriteTextDocument: ...

    async def generate_rewrite(self, request: RewriteGenerationRequest) -> RewriteDocument: ...

    async def generate_translation(
        self, request: TranslationGenerationRequest
    ) -> TranslationDocument: ...

    async def generate_document(self, request: DocumentGenerationRequest) -> PreparedDocument: ...
