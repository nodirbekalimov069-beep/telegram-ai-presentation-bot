import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from apps.worker import tasks
from core.config import Settings
from core.database.base import Base
from modules.ai.providers import openai_compatible
from modules.ai.providers.errors import ProviderCallBudgetExhausted
from modules.ai.providers.mock import MockAIProvider
from modules.ai.quiz_contracts import QuizDocument, QuizGenerationRequest
from modules.files.storage import LocalStorage
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


class _SimulatedProcessDeath(BaseException):
    pass


async def _quiz_actor_state(tmp_path: Path) -> tuple[async_sessionmaker[AsyncSession], int]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quiz-actor.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=2771,
            username="quiz-actor-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    async with factory() as session:
        order = await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=2771,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": "uz",
                "requirements": "Amaliy vaziyatlarga e’tibor bering",
            },
            idempotency_key="quiz-actor-budget",
        )
    return factory, order.id


async def _saved_order(factory: async_sessionmaker[AsyncSession], order_id: int) -> ServiceOrder:
    async with factory() as session:
        order = await session.get(ServiceOrder, order_id)
        assert order is not None
        session.expunge(order)
        return order


def _assert_terminal_without_partial_output(saved: ServiceOrder) -> None:
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "AI_PROVIDER_BUDGET_EXHAUSTED"
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.result_metadata == {}
    assert saved.delivery_status == "PENDING"
    assert saved.delivery_attempts == 0
    assert saved.docx_telegram_message_id is None
    assert saved.pdf_telegram_message_id is None
    assert saved.generation_lease_token is None


def _settings(tmp_path: Path, *, ai_max_retries: int) -> Settings:
    return Settings(
        ai_provider="openai_compatible",
        ai_api_key="test-key",
        ai_base_url="https://example.test/v1",
        ai_model="test-model",
        ai_max_retries=ai_max_retries,
        ai_retry_base_delay_seconds=0,
        generation_work_root=tmp_path / "work",
        local_storage_root=tmp_path / "storage",
    )


def _wire_production_actor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    factory: async_sessionmaker[AsyncSession],
    *,
    ai_max_retries: int,
    outbound: AsyncMock,
) -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=outbound)),
        close=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(openai_compatible, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(tmp_path, ai_max_retries=ai_max_retries)
    )
    monkeypatch.setattr(tasks, "create_engine", lambda settings: engine)
    monkeypatch.setattr(tasks, "create_session_factory", lambda current: factory)
    monkeypatch.setattr(tasks, "build_storage", lambda settings: LocalStorage(tmp_path / "storage"))


def _run_until_terminal(order_id: int) -> None:
    for _ in range(8):
        try:
            tasks._run_service_quiz_actor(order_id)
        except tasks.GenerationAttemptError:
            continue
        return
    raise AssertionError("quiz actor did not reach terminal state")


def _wire_fake_provider_actor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    factory: async_sessionmaker[AsyncSession],
    provider: MockAIProvider,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(tmp_path, ai_max_retries=10))
    monkeypatch.setattr(tasks, "create_engine", lambda settings: engine)
    monkeypatch.setattr(tasks, "create_session_factory", lambda current: factory)
    monkeypatch.setattr(tasks, "build_ai_provider", lambda settings: provider)
    monkeypatch.setattr(tasks, "close_ai_provider", AsyncMock())
    monkeypatch.setattr(tasks, "build_storage", lambda settings: LocalStorage(tmp_path / "storage"))


@pytest.mark.parametrize("ai_max_retries", [0, 1, 2, 3, 4, 10])
def test_production_quiz_timeout_budget_is_durable_and_exactly_four_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ai_max_retries: int,
) -> None:
    factory, order_id = asyncio.run(_quiz_actor_state(tmp_path))
    outbound = AsyncMock(
        side_effect=APITimeoutError(
            request=httpx.Request("POST", "https://example.test/v1/chat/completions")
        )
    )
    _wire_production_actor(
        monkeypatch,
        tmp_path,
        factory,
        ai_max_retries=ai_max_retries,
        outbound=outbound,
    )

    _run_until_terminal(order_id)
    tasks._run_service_quiz_actor(order_id)

    saved = asyncio.run(_saved_order(factory, order_id))
    assert outbound.await_count == 4
    assert saved.generation_metadata["quiz_provider_calls"] == 4
    _assert_terminal_without_partial_output(saved)


@pytest.mark.parametrize(
    "sequence",
    [
        ("duplicate", "duplicate", "duplicate", "duplicate"),
        ("timeout", "timeout", "duplicate", "timeout"),
        ("duplicate", "timeout", "timeout", "timeout"),
    ],
)
def test_production_quiz_mixed_failures_share_one_durable_four_call_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sequence: tuple[str, ...],
) -> None:
    factory, order_id = asyncio.run(_quiz_actor_state(tmp_path))
    request = QuizGenerationRequest(
        topic="Algoritmik fikrlash",
        question_count=4,
        difficulty="medium",
        option_count=4,
        answer_key=True,
        language="uz",
        additional_requirements="Amaliy vaziyatlarga e’tibor bering",
    )
    unique = asyncio.run(MockAIProvider().generate_quiz(request))
    duplicate_payload = unique.model_dump()
    duplicate_payload["questions"][1]["text"] = duplicate_payload["questions"][0]["text"]
    with pytest.raises(ValidationError, match="quiz questions must be unique") as duplicate:
        QuizDocument.model_validate(duplicate_payload)
    timeout = APITimeoutError(
        request=httpx.Request("POST", "https://example.test/v1/chat/completions")
    )
    effects = [duplicate.value if kind == "duplicate" else timeout for kind in sequence]
    outbound = AsyncMock(side_effect=effects)
    _wire_production_actor(
        monkeypatch,
        tmp_path,
        factory,
        ai_max_retries=10,
        outbound=outbound,
    )

    _run_until_terminal(order_id)
    tasks._run_service_quiz_actor(order_id)

    saved = asyncio.run(_saved_order(factory, order_id))
    assert outbound.await_count == 4
    assert saved.generation_metadata["quiz_provider_calls"] == 4
    _assert_terminal_without_partial_output(saved)


def test_process_death_and_expired_lease_cannot_reset_durable_quiz_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory, order_id = asyncio.run(_quiz_actor_state(tmp_path))
    timeout = APITimeoutError(
        request=httpx.Request("POST", "https://example.test/v1/chat/completions")
    )
    outbound = AsyncMock(side_effect=[timeout, _SimulatedProcessDeath(), timeout, timeout])
    _wire_production_actor(
        monkeypatch,
        tmp_path,
        factory,
        ai_max_retries=10,
        outbound=outbound,
    )

    with pytest.raises(_SimulatedProcessDeath):
        tasks._run_service_quiz_actor(order_id)
    after_crash = asyncio.run(_saved_order(factory, order_id))
    assert outbound.await_count == 2
    assert after_crash.generation_metadata["quiz_provider_calls"] == 2
    assert after_crash.status == ServiceOrderStatus.GENERATING

    async def expire_lease() -> None:
        async with factory() as session, session.begin():
            order = await session.get(ServiceOrder, order_id)
            assert order is not None
            order.generation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    asyncio.run(expire_lease())
    tasks._run_service_quiz_actor(order_id)
    tasks._run_service_quiz_actor(order_id)

    saved = asyncio.run(_saved_order(factory, order_id))
    assert outbound.await_count == 4
    assert saved.generation_metadata["quiz_provider_calls"] == 4
    _assert_terminal_without_partial_output(saved)


def test_non_openai_budget_exhaustion_is_terminal_at_real_actor_and_redelivery_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory, order_id = asyncio.run(_quiz_actor_state(tmp_path))
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(
        side_effect=ProviderCallBudgetExhausted("fake provider exhausted")
    )  # type: ignore[method-assign]
    _wire_fake_provider_actor(monkeypatch, tmp_path, factory, provider)

    tasks._run_service_quiz_actor(order_id)
    tasks._run_service_quiz_actor(order_id)

    saved = asyncio.run(_saved_order(factory, order_id))
    assert provider.generate_quiz.await_count == 1  # type: ignore[attr-defined]
    _assert_terminal_without_partial_output(saved)


def test_non_openai_ordinary_failure_keeps_real_actor_retry_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory, order_id = asyncio.run(_quiz_actor_state(tmp_path))
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(side_effect=ConnectionError("temporary outage"))  # type: ignore[method-assign]
    _wire_fake_provider_actor(monkeypatch, tmp_path, factory, provider)

    with pytest.raises(tasks.GenerationAttemptError):
        tasks._run_service_quiz_actor(order_id)

    saved = asyncio.run(_saved_order(factory, order_id))
    assert provider.generate_quiz.await_count == 1  # type: ignore[attr-defined]
    assert saved.status == ServiceOrderStatus.QUEUED
    assert saved.error_code == "AI_GENERATION_FAILED"
    assert saved.retry_count == 1
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.delivery_attempts == 0
