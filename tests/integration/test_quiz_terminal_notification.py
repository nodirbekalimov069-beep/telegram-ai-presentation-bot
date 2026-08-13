from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.service_orders.essay_pipeline import mark_terminal_quiz_failure
from modules.service_orders.models import ServiceOrder
from modules.service_orders.quiz_notifications import (
    notify_quiz_progress,
    notify_terminal_quiz_failure,
)
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def notification_db(
    tmp_path: Path,
) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quiz-notification.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _create_failed_order(
    factory: async_sessionmaker[AsyncSession], *, language: str
) -> ServiceOrder:
    telegram_user_id = {"uz": 4101, "ru": 4102, "en": 4103}[language]
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=telegram_user_id,
            username=f"quiz-{language}",
            first_name="Owner",
            last_name=None,
            language_code=language,
        )
    async with factory() as session:
        order = await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=telegram_user_id,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": language,
                "requirements": "",
            },
            idempotency_key=f"quiz-terminal-{language}",
            progress_chat_id=telegram_user_id,
            progress_message_id=9000 + telegram_user_id,
            progress_language=language,
        )
    async with factory() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.generation_metadata = {
            **saved.generation_metadata,
            "quiz_provider_calls": 4,
        }
    assert await mark_terminal_quiz_failure(
        factory,
        order_id=order.id,
        error_code="AI_PROVIDER_BUDGET_EXHAUSTED",
        user_message="safe failure",
    )
    async with factory() as session:
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        return saved


@pytest.mark.asyncio
async def test_progress_is_durable_before_generation_and_redelivery_is_noop(
    notification_db: async_sessionmaker[AsyncSession],
) -> None:
    telegram_user_id = 4201
    async with notification_db() as session:
        await UserService().register(
            session,
            telegram_user_id=telegram_user_id,
            username="quiz-progress",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    async with notification_db() as session:
        order = await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=telegram_user_id,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": "uz",
                "requirements": "",
            },
            idempotency_key="quiz-progress-uz",
            progress_chat_id=telegram_user_id,
            progress_message_id=14201,
            progress_language="uz",
        )
    bot = AsyncMock()
    assert await notify_quiz_progress(notification_db, bot=bot, order_id=order.id)
    assert not await notify_quiz_progress(notification_db, bot=bot, order_id=order.id)
    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    async with notification_db() as session:
        saved = await session.get(ServiceOrder, order.id)
        event_types = list(
            await session.scalars(
                select(OutboxEvent.event_type).where(OutboxEvent.aggregate_id == str(order.id))
            )
        )
    assert saved is not None
    assert saved.generation_metadata["quiz_progress_status"] == "DELIVERED"
    assert event_types.count("service.quiz.progress_requested") == 1
    assert event_types.count("service.quiz.requested") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("uz", "❌ Quizni xavfsiz tayyorlab bo‘lmadi. Iltimos, keyinroq qayta urinib ko‘ring."),
        ("ru", "❌ Не удалось безопасно подготовить тест. Пожалуйста, повторите попытку позже."),
        ("en", "❌ The quiz could not be prepared safely. Please try again later."),
    ],
)
async def test_terminal_quiz_failure_notification_is_localized_durable_and_exact_once(
    notification_db: async_sessionmaker[AsyncSession],
    language: str,
    expected: str,
) -> None:
    order = await _create_failed_order(notification_db, language=language)
    bot = AsyncMock()

    assert await notify_terminal_quiz_failure(notification_db, bot=bot, order_id=order.id)
    assert not await notify_terminal_quiz_failure(notification_db, bot=bot, order_id=order.id)

    bot.edit_message_text.assert_awaited_once_with(
        chat_id={"uz": 4101, "ru": 4102, "en": 4103}[language],
        message_id=9000 + {"uz": 4101, "ru": 4102, "en": 4103}[language],
        text=expected,
    )
    bot.send_message.assert_not_awaited()
    async with notification_db() as session:
        saved = await session.get(ServiceOrder, order.id)
        events = list(
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "service.quiz.terminal_notification_requested",
                    OutboxEvent.aggregate_id == str(order.id),
                )
            )
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.generation_metadata["quiz_provider_calls"] == 4
    assert saved.generation_metadata["quiz_terminal_notification_status"] == "DELIVERED"
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.delivery_attempts == 0
    assert len(events) == 1
    assert order_count == 1


@pytest.mark.asyncio
async def test_terminal_notification_redelivery_has_zero_telegram_and_order_delta(
    notification_db: async_sessionmaker[AsyncSession],
) -> None:
    order = await _create_failed_order(notification_db, language="uz")
    bot = AsyncMock()
    assert await notify_terminal_quiz_failure(notification_db, bot=bot, order_id=order.id)
    before_calls = bot.edit_message_text.await_count

    for _ in range(3):
        assert not await notify_terminal_quiz_failure(notification_db, bot=bot, order_id=order.id)

    assert bot.edit_message_text.await_count == before_calls
    bot.send_message.assert_not_awaited()
    async with notification_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.generation_metadata["quiz_provider_calls"] == 4
    assert saved.result_metadata == {}
    assert saved.delivery_attempts == 0


@pytest.mark.asyncio
async def test_non_budget_quiz_failure_does_not_emit_terminal_notification(
    notification_db: async_sessionmaker[AsyncSession],
) -> None:
    telegram_user_id = 4301
    async with notification_db() as session:
        await UserService().register(
            session,
            telegram_user_id=telegram_user_id,
            username="quiz-other-failure",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    async with notification_db() as session:
        order = await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=telegram_user_id,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": "uz",
                "requirements": "",
            },
            idempotency_key="quiz-other-failure",
            progress_chat_id=telegram_user_id,
            progress_message_id=14301,
            progress_language="uz",
        )
    assert await mark_terminal_quiz_failure(
        notification_db,
        order_id=order.id,
        error_code="DOCX_RENDER_FAILED",
        user_message="safe failure",
    )
    async with notification_db() as session:
        count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == "service.quiz.terminal_notification_requested",
                OutboxEvent.aggregate_id == str(order.id),
            )
        )
    assert count == 0
