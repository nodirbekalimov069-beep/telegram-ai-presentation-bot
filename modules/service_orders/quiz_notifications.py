"""Durable, idempotent terminal progress notification for failed QUIZ orders."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.localization import Localizer
from core.outbox.service import OutboxService
from modules.service_orders.catalog import ServiceType
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderStatus

_LOCALIZER = Localizer.load_default()


async def notify_quiz_progress(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    bot: Any,
    order_id: int,
) -> bool:
    """Durably establish progress before generation can be published."""
    async with session_factory() as session, session.begin():
        order = await session.scalar(
            select(ServiceOrder).where(ServiceOrder.id == order_id).with_for_update()
        )
        if order is None or order.service_type != ServiceType.QUIZ.value:
            return False
        metadata = dict(order.generation_metadata)
        if metadata.get("quiz_progress_status") == "DELIVERED":
            return False
        destination = metadata.get("quiz_progress")
        if not isinstance(destination, dict):
            return False
        chat_id = destination.get("chat_id")
        message_id = destination.get("message_id")
        language = destination.get("language")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or language not in _LOCALIZER.languages
        ):
            return False
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_LOCALIZER.text(str(language), "service.quiz_created"),
        )
        metadata["quiz_progress_status"] = "DELIVERED"
        order.generation_metadata = metadata
        await OutboxService().enqueue(
            session,
            event_type="service.quiz.requested",
            aggregate_type="service_order",
            aggregate_id=str(order.id),
            idempotency_key=f"service-quiz:{order.id}:1",
            payload={"service_order_id": order.id},
        )
        return True


async def notify_terminal_quiz_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    bot: Any,
    order_id: int,
) -> bool:
    """Edit one durable progress message; completed redelivery is a no-op.

    The row lock spans the idempotent Telegram edit so parallel workers cannot
    race. A crash rolls the checkpoint back; retrying the same edit on the same
    message cannot create a duplicate Telegram message.
    """

    async with session_factory() as session, session.begin():
        order = await session.scalar(
            select(ServiceOrder).where(ServiceOrder.id == order_id).with_for_update()
        )
        if (
            order is None
            or order.service_type != ServiceType.QUIZ.value
            or order.status != ServiceOrderStatus.FAILED
            or order.error_code != "AI_PROVIDER_BUDGET_EXHAUSTED"
        ):
            return False
        metadata = dict(order.generation_metadata)
        if metadata.get("quiz_terminal_notification_status") == "DELIVERED":
            return False
        destination = metadata.get("quiz_progress")
        if not isinstance(destination, dict):
            return False
        chat_id = destination.get("chat_id")
        message_id = destination.get("message_id")
        language = destination.get("language")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or language not in _LOCALIZER.languages
        ):
            return False
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_LOCALIZER.text(str(language), "service.quiz_failure"),
        )
        metadata["quiz_terminal_notification_status"] = "DELIVERED"
        order.generation_metadata = metadata
        return True
