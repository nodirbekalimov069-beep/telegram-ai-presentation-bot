from typing import Literal
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from core.config import Settings
from modules.ai.course_work_contracts import (
    COURSE_WORK_AUTOMATIC_SECTIONS,
    CourseWorkDocument,
    CourseWorkGenerationRequest,
)
from modules.ai.factory import build_ai_provider
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider


async def test_mock_provider_generates_valid_bound_course_work() -> None:
    request = CourseWorkGenerationRequest(
        subject="Pedagogika",
        topic="Hamkorlikdagi o‘rganish",
        sections="Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
        language="uz",
        length="short",
        additional_requirements="Xulosada xavfsiz hamkorlik mezonlarini tushuntir.",
    )

    result = await MockAIProvider().generate_course_work(request)

    assert result.title == request.topic
    assert result.subject == request.subject
    assert result.language == request.language
    assert [chapter.heading for chapter in result.chapters] == [
        "Nazariy asoslar",
        "Amaliy tahlil",
        "Tavsiyalar",
    ]
    assert "xavfsiz hamkorlik mezonlarini" in result.conclusion
    assert result.references == []


@pytest.mark.parametrize(
    ("language", "subject", "topic", "sections", "requirements"),
    [
        (
            "en",
            "Computer Science",
            "Algorithmic thinking",
            "Theory; Practice; Recommendations",
            "Explain responsible automation through human oversight.",
        ),
        (
            "en",
            "Energy Systems",
            "Renewable energy storage and grid integration",
            "Storage technologies; Grid integration; Risk controls",
            "Explain renewable energy storage and grid integration in practical terms.",
        ),
        (
            "ru",
            "Информатика",
            "Алгоритмическое мышление",
            "Теория; Практика; Рекомендации",
            "Объясните ответственную автоматизацию через человеческий контроль.",
        ),
    ],
)
async def test_mock_provider_preserves_valid_english_and_russian_prose(
    language: Literal["ru", "en"],
    subject: str,
    topic: str,
    sections: str,
    requirements: str,
) -> None:
    request = CourseWorkGenerationRequest(
        subject=subject,
        topic=topic,
        sections=sections,
        language=language,
        length="short",
        additional_requirements=requirements,
    )

    result = await MockAIProvider().generate_course_work(request)

    assert result.language == language
    assert result.requirement_evidence


@pytest.mark.parametrize(
    ("language", "subject", "topic"),
    [
        ("uz", "Informatika", "Algoritmik fikrlash"),
        ("ru", "Информатика", "Алгоритмическое мышление"),
        ("en", "Computer Science", "Algorithmic thinking"),
    ],
)
async def test_production_mock_automatic_course_sections_are_localized(
    language: Literal["uz", "ru", "en"], subject: str, topic: str
) -> None:
    provider = build_ai_provider(Settings(ai_provider="mock", ai_api_key=SecretStr("test-key")))
    provider.generate_course_work = AsyncMock(wraps=provider.generate_course_work)  # type: ignore[method-assign]
    request = CourseWorkGenerationRequest(
        subject=subject,
        topic=topic,
        sections=COURSE_WORK_AUTOMATIC_SECTIONS,
        language=language,
        length="short",
    )

    result = await provider.generate_course_work(request)
    headings = [chapter.heading for chapter in result.chapters]

    assert provider.generate_course_work.await_count == 1  # type: ignore[attr-defined]
    assert 2 <= len(headings) <= 12
    assert len({" ".join(item.casefold().split()) for item in headings}) == len(headings)
    assert COURSE_WORK_AUTOMATIC_SECTIONS not in headings
    if language in {"ru", "en"}:
        assert not {"Nazariy asoslar", "Amaliy tahlil", "Tavsiyalar"}.intersection(headings)


@pytest.mark.parametrize(
    ("language", "subject", "topic", "sections"),
    [
        (
            "ru",
            "Информатика",
            "Алгоритмическое мышление",
            ("Авторский обзор", "Практический разбор", "Контроль качества"),
        ),
        (
            "en",
            "Computer Science",
            "Algorithmic thinking",
            ("Custom overview", "Applied investigation", "Quality controls"),
        ),
    ],
)
async def test_production_mock_preserves_explicit_course_sections_exactly(
    language: Literal["ru", "en"],
    subject: str,
    topic: str,
    sections: tuple[str, ...],
) -> None:
    provider = build_ai_provider(Settings(ai_provider="mock", ai_api_key=SecretStr("test-key")))
    provider.generate_course_work = AsyncMock(wraps=provider.generate_course_work)  # type: ignore[method-assign]
    request = CourseWorkGenerationRequest(
        subject=subject,
        topic=topic,
        sections="; ".join(sections),
        language=language,
        length="short",
    )

    result = await provider.generate_course_work(request)

    assert provider.generate_course_work.await_count == 1  # type: ignore[attr-defined]
    assert tuple(chapter.heading for chapter in result.chapters) == sections


async def test_openai_provider_uses_strict_course_work_schema_and_validator() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        max_retries=0,
    )
    request = CourseWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmik fikrlash",
        sections="Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
        language="uz",
        length="short",
    )
    expected = await MockAIProvider().generate_course_work(request)
    provider._request_structured = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await provider.generate_course_work(request)

    assert result is expected
    call = provider._request_structured.await_args.kwargs  # type: ignore[attr-defined]
    assert call["response_format"] is CourseWorkDocument
    assert call["response_validator"](expected) is expected
    messages = list(call["messages"])
    assert request.subject in messages[1]["content"]
    assert request.topic in messages[1]["content"]
    assert request.sections in messages[1]["content"]
    await provider.aclose()
