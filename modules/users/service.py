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


MAX_FREE_DEMO_ATTEMPTS = 3


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
        private_owner_test: bool = False,
    ) -> Order:
        trusted_settings = dict(settings)
        trusted_settings.pop("_private_owner_test", None)
        try:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
                )
                if (
                    user is None
                    or user.is_blocked
                    or (user.free_demo_used and not private_owner_test)
                ):
                    raise FreeDemoUnavailable("Free Demo is unavailable for this Telegram user")

                plan = await session.scalar(
                    select(Plan).where(Plan.code == "free_demo", Plan.is_active.is_(True))
                )
                if plan is None:
                    raise FreeDemoUnavailable("Free Demo plan is disabled")
                slide_count = int(settings.get("slide_count", 0))
                maximum_slides = 30 if private_owner_test else plan.maximum_slides
                if not plan.minimum_slides <= slide_count <= maximum_slides:
                    raise FreeDemoUnavailable("requested slide count violates Free Demo limits")

                if private_owner_test:
                    trusted_settings["_private_owner_test"] = True
                    order = Order(
                        user_id=user.id,
                        plan_id=plan.id,
                        amount=0,
                        currency="UZS",
                        status=OrderStatus.CREATED,
                        topic=topic,
                        settings=trusted_settings,
                    )
                    session.add(order)
                    await session.flush()
                    return order

                claim = await session.scalar(
                    select(FreeDemoClaim)
                    .where(FreeDemoClaim.telegram_user_id == telegram_user_id)
                    .with_for_update()
                )
                if claim is not None:
                    if claim.status != "RELEASED":
                        raise FreeDemoUnavailable("Free Demo has already been reserved or consumed")
                    if claim.attempt_count >= MAX_FREE_DEMO_ATTEMPTS:
                        raise FreeDemoUnavailable("Free Demo retry limit has been reached")

                order = Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    amount=0,
                    currency="UZS",
                    status=OrderStatus.CREATED,
                    topic=topic,
                    settings=trusted_settings,
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
        """Consume the entitlement once a validated AI outline is persisted."""
        async with session.begin():
            await self.consume_reserved_in_transaction(session, order_id=order_id)

    async def consume_reserved_in_transaction(
        self,
        session: AsyncSession,
        *,
        order_id: int,
    ) -> None:
        """Consume a reservation inside the caller's active transaction."""
        reference = await session.scalar(
            select(FreeDemoClaim).where(FreeDemoClaim.order_id == order_id)
        )
        if reference is None:
            return
        user = await session.get(User, reference.user_id, with_for_update=True)
        claim = await session.scalar(
            select(FreeDemoClaim)
            .where(FreeDemoClaim.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if claim is None:
            return
        if claim.status == "CONSUMED":
            return
        if claim.status != "RESERVED":
            raise FreeDemoUnavailable("Free Demo reservation is not consumable")
        claim.status = "CONSUMED"
        claim.consumed_at = datetime.now(UTC)
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
            order = await session.scalar(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None or OrderStatus(order.status) != OrderStatus.FAILED:
                return False
            return await self.release_system_failure_in_transaction(
                session,
                order_id=order_id,
                reason=reason,
            )

    async def release_system_failure_in_transaction(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        reason: str,
    ) -> bool:
        """Release a reservation after the caller locks and marks its order FAILED."""
        claim = await session.scalar(
            select(FreeDemoClaim)
            .where(FreeDemoClaim.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if claim is None or claim.status != "RESERVED":
            return False
        claim.status = "RELEASED"
        claim.released_at = datetime.now(UTC)
        claim.release_reason = reason[:500]
        return True

    async def release_cancelled(self, session: AsyncSession, *, order_id: int) -> bool:
        """Release an unconsumed reservation after a user cancels the outline."""
        async with session.begin():
            order = await session.scalar(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None or OrderStatus(order.status) != OrderStatus.CANCELLED:
                return False
            return await self.release_cancelled_in_transaction(session, order_id=order_id)

    async def release_cancelled_in_transaction(
        self,
        session: AsyncSession,
        *,
        order_id: int,
    ) -> bool:
        """Release a reservation after the caller locks and cancels its order."""
        claim = await session.scalar(
            select(FreeDemoClaim)
            .where(FreeDemoClaim.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if claim is None or claim.status != "RESERVED":
            return False
        claim.status = "RELEASED"
        claim.released_at = datetime.now(UTC)
        claim.release_reason = "cancelled by user"
        return True
