"""Presentation outline and queue application service."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.outbox.service import OutboxService
from modules.ai.contracts import PresentationRequest
from modules.ai.providers.base import AIProvider
from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.models import GenerationJob, Presentation
from modules.users.service import FreeDemoService


class PresentationService:
    def __init__(self, *, ai_provider: AIProvider | None = None) -> None:
        self._ai_provider = ai_provider
        self._machine = OrderStateMachine()
        self._outbox = OutboxService()

    async def generate_outline(self, session: AsyncSession, *, order_id: int) -> Presentation:
        if self._ai_provider is None:
            raise RuntimeError("AI provider is required for outline generation")
        async with session.begin():
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                raise LookupError(f"order {order_id} not found")
            current = OrderStatus(order.status)
            order.status = self._machine.transition(current, OrderStatus.OUTLINE_GENERATING)
            presentation = await session.scalar(
                select(Presentation).where(Presentation.order_id == order.id)
            )
            settings = order.settings
            if presentation is None:
                presentation = Presentation(
                    order_id=order.id,
                    user_id=order.user_id,
                    title=order.topic,
                    topic=order.topic,
                    language=str(settings.get("language", "uz")),
                    direction=str(settings.get("direction", "Boshqa yo‘nalish")),
                    audience=str(settings.get("audience", "Umumiy auditoriya")),
                    slide_count=int(settings["slide_count"]),
                    design_style=str(settings.get("design_style", "ai")),
                    outline={},
                    content={},
                    status=OrderStatus.OUTLINE_GENERATING,
                )
                session.add(presentation)
                await session.flush()
            else:
                presentation.status = OrderStatus.OUTLINE_GENERATING
            request = PresentationRequest(
                topic=order.topic,
                language=presentation.language,
                direction=presentation.direction,
                purpose=str(settings.get("purpose", "Ma’lumot berish")),
                audience=presentation.audience,
                knowledge_level=str(settings.get("knowledge_level", "beginner")),
                slide_count=presentation.slide_count,
                design_style=presentation.design_style,
            )
            presentation_id = presentation.id

        try:
            outline = await self._ai_provider.generate_outline(request)
        except Exception as exc:
            async with session.begin():
                order = await session.get(Order, order_id, with_for_update=True)
                presentation = await session.get(
                    Presentation, presentation_id, with_for_update=True
                )
                if (
                    order is not None
                    and OrderStatus(order.status) == OrderStatus.OUTLINE_GENERATING
                ):
                    order.status = self._machine.transition(
                        OrderStatus.OUTLINE_GENERATING,
                        OrderStatus.FAILED,
                    )
                if presentation is not None:
                    presentation.status = OrderStatus.FAILED
                    presentation.error_message = f"{type(exc).__name__}: outline generation failed"
            await FreeDemoService().release_system_failure(
                session,
                order_id=order_id,
                reason=f"{type(exc).__name__}: outline generation failed",
            )
            raise

        async with session.begin():
            order = await session.get(Order, order_id, with_for_update=True)
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            if order is None or presentation is None:
                raise LookupError("order or presentation disappeared during outline generation")
            order.status = self._machine.transition(
                OrderStatus(order.status),
                OrderStatus.WAITING_OUTLINE_APPROVAL,
            )
            presentation.outline = outline.model_dump(mode="json")
            presentation.status = OrderStatus.WAITING_OUTLINE_APPROVAL
        return presentation

    async def approve_outline(
        self,
        session: AsyncSession,
        *,
        presentation_id: int,
        progress_chat_id: int | None = None,
        progress_message_id: int | None = None,
    ) -> GenerationJob:
        async with session.begin():
            presentation = await session.scalar(
                select(Presentation).where(Presentation.id == presentation_id).with_for_update()
            )
            if presentation is None:
                raise LookupError(f"presentation {presentation_id} not found")
            order = await session.get(Order, presentation.order_id, with_for_update=True)
            if order is None:
                raise LookupError("presentation order not found")
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.presentation_id == presentation.id)
            )
            if job is not None:
                return job
            current = OrderStatus(order.status)
            if current != OrderStatus.QUEUED:
                order.status = self._machine.transition(
                    current,
                    OrderStatus.QUEUED,
                )
            presentation.status = OrderStatus.QUEUED
            plan = await session.get(Plan, order.plan_id)
            job = GenerationJob(
                presentation_id=presentation.id,
                status="QUEUED",
                progress=0,
                current_step="queued",
                progress_chat_id=progress_chat_id,
                progress_message_id=progress_message_id,
                retries=0,
                priority=plan.priority if plan is not None else 0,
            )
            session.add(job)
            await session.flush()
            await self._outbox.enqueue(
                session,
                event_type="generation.requested",
                aggregate_type="presentation",
                aggregate_id=str(presentation.id),
                idempotency_key=f"generation:{presentation.id}",
                payload={
                    "presentation_id": presentation.id,
                    "priority": job.priority,
                },
            )
        return job
