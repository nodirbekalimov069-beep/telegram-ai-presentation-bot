"""User registration and Free Demo application services."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.users.models import FreeDemoClaim, User


class FreeDemoUnavailable(RuntimeError):
    """Raised when the Telegram ID has no available Free Demo entitlement."""


class UserService:
    async def register(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str | None,
    ) -> User:
        async with session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            selected_language = language_code if language_code in {"uz", "ru", "en"} else "uz"
            if user is None:
                user = User(
                    telegram_user_id=telegram_user_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    language_code=language_code,
                    selected_language=selected_language,
                )
                session.add(user)
            else:
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.language_code = language_code
                user.last_activity_at = datetime.now(UTC)
            await session.flush()
        return user


class FreeDemoService:
    async def claim(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        topic: str,
        settings: dict[str, Any],
    ) -> Order:
        try:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
                )
                if user is None or user.is_blocked or user.free_demo_used:
                    raise FreeDemoUnavailable("Free Demo is unavailable for this Telegram user")

                plan = await session.scalar(
                    select(Plan).where(Plan.code == "free_demo", Plan.is_active.is_(True))
                )
                if plan is None:
                    raise FreeDemoUnavailable("Free Demo plan is disabled")
                slide_count = int(settings.get("slide_count", 0))
                if not plan.minimum_slides <= slide_count <= plan.maximum_slides:
                    raise FreeDemoUnavailable("requested slide count violates Free Demo limits")

                claim = await session.scalar(
                    select(FreeDemoClaim)
                    .where(FreeDemoClaim.telegram_user_id == telegram_user_id)
                    .with_for_update()
                )
                if claim is not None and claim.status != "RELEASED":
                    raise FreeDemoUnavailable("Free Demo has already been reserved or consumed")

                order = Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    amount=0,
                    currency="UZS",
                    status=OrderStatus.CREATED,
                    topic=topic,
                    settings=settings,
                )
                session.add(order)
                await session.flush()
                now = datetime.now(UTC)
                if claim is None:
                    claim = FreeDemoClaim(
                        telegram_user_id=telegram_user_id,
                        user_id=user.id,
                        order_id=order.id,
                        status="RESERVED",
                        reserved_at=now,
                    )
                    session.add(claim)
                else:
                    claim.user_id = user.id
                    claim.order_id = order.id
                    claim.status = "RESERVED"
                    claim.attempt_count += 1
                    claim.reserved_at = now
                    claim.consumed_at = None
                    claim.released_at = None
                    claim.release_reason = None
                await session.flush()
            return order
        except IntegrityError as exc:
            await session.rollback()
            raise FreeDemoUnavailable("Free Demo is already reserved") from exc

    async def consume_reserved(self, session: AsyncSession, *, order_id: int) -> None:
        """Consume the entitlement only once generation has successfully started."""
        async with session.begin():
            claim = await session.scalar(
                select(FreeDemoClaim).where(FreeDemoClaim.order_id == order_id).with_for_update()
            )
            if claim is None:
                return
            if claim.status == "CONSUMED":
                return
            if claim.status != "RESERVED":
                raise FreeDemoUnavailable("Free Demo reservation is not consumable")
            claim.status = "CONSUMED"
            claim.consumed_at = datetime.now(UTC)
            user = await session.get(User, claim.user_id, with_for_update=True)
            if user is not None:
                user.free_demo_used = True

    async def release_system_failure(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        reason: str,
    ) -> bool:
        """Release only an unconsumed reservation after a documented system failure."""
        async with session.begin():
            claim = await session.scalar(
                select(FreeDemoClaim).where(FreeDemoClaim.order_id == order_id).with_for_update()
            )
            if claim is None or claim.status != "RESERVED":
                return False
            order = await session.get(Order, order_id, with_for_update=True)
            if order is None or OrderStatus(order.status) != OrderStatus.FAILED:
                return False
            claim.status = "RELEASED"
            claim.released_at = datetime.now(UTC)
            claim.release_reason = reason[:500]
            return True
