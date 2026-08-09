"""Transactional application service for preview service orders."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.outbox.service import OutboxService
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType, validate_service_request
from modules.service_orders.models import ServiceOrder
from modules.users.models import User


class ServiceOrderStatus(StrEnum):
    PREVIEW = "PREVIEW"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    GENERATING = "GENERATING"
    DOCUMENT_RENDERING = "DOCUMENT_RENDERING"
    ARTIFACTS_READY = "ARTIFACTS_READY"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _payload_hash(service_type: ServiceType, payload: dict[str, Any]) -> str:
    value = json.dumps(
        {"service_type": service_type.value, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _short_title(value: Any) -> str:
    text = " ".join(str(value).split())
    return text[:197] + "…" if len(text) > 200 else text


class ServiceOrderService:
    async def create_preview_order(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        service_type: ServiceType | str,
        request_payload: dict[str, Any],
        idempotency_key: str,
    ) -> ServiceOrder:
        if telegram_user_id <= 0:
            raise PermissionError("invalid Telegram identity")
        if not 8 <= len(idempotency_key) <= 64 or not idempotency_key.isascii():
            raise ValueError("invalid idempotency key")
        canonical_type = ServiceType(service_type)
        payload = validate_service_request(canonical_type, request_payload)
        request_hash = _payload_hash(canonical_type, payload)

        try:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
                )
                if user is None or user.is_blocked:
                    raise PermissionError("user is missing or blocked")
                existing = await session.scalar(
                    select(ServiceOrder).where(ServiceOrder.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return self._validated_duplicate(
                        existing,
                        user_id=user.id,
                        service_type=canonical_type,
                        request_hash=request_hash,
                    )
                definition = SERVICE_CATALOG[canonical_type]
                order = ServiceOrder(
                    user_id=user.id,
                    service_type=canonical_type.value,
                    status=ServiceOrderStatus.PREVIEW,
                    title=_short_title(payload[definition.title_field]),
                    request_payload=payload,
                    normalized_request=payload,
                    request_hash=request_hash,
                    generation_metadata={},
                    result_metadata={},
                    idempotency_key=idempotency_key,
                )
                session.add(order)
                await session.flush()
                return order
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(ServiceOrder)
                .join(User, User.id == ServiceOrder.user_id)
                .where(
                    ServiceOrder.idempotency_key == idempotency_key,
                    User.telegram_user_id == telegram_user_id,
                )
            )
            if existing is None:
                raise
            return self._validated_duplicate(
                existing,
                user_id=existing.user_id,
                service_type=canonical_type,
                request_hash=request_hash,
            )

    async def create_essay_order(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        request_payload: dict[str, Any],
        idempotency_key: str,
    ) -> ServiceOrder:
        return await self._create_production_document_order(
            session,
            telegram_user_id=telegram_user_id,
            service_type=ServiceType.ESSAY,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
        )

    async def create_independent_work_order(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        request_payload: dict[str, Any],
        idempotency_key: str,
    ) -> ServiceOrder:
        return await self._create_production_document_order(
            session,
            telegram_user_id=telegram_user_id,
            service_type=ServiceType.INDEPENDENT_WORK,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
        )

    async def _create_production_document_order(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        service_type: ServiceType,
        request_payload: dict[str, Any],
        idempotency_key: str,
    ) -> ServiceOrder:
        from modules.service_orders.document_services import document_service_profile

        if telegram_user_id <= 0:
            raise PermissionError("invalid Telegram identity")
        if not 8 <= len(idempotency_key) <= 64 or not idempotency_key.isascii():
            raise ValueError("invalid idempotency key")
        profile = document_service_profile(service_type)
        payload = validate_service_request(service_type, request_payload)
        request_hash = _payload_hash(service_type, payload)
        now = datetime.now(UTC)
        try:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
                )
                if user is None or user.is_blocked:
                    raise PermissionError("user is missing or blocked")
                existing = await session.scalar(
                    select(ServiceOrder).where(ServiceOrder.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return self._validated_duplicate(
                        existing,
                        user_id=user.id,
                        service_type=service_type,
                        request_hash=request_hash,
                    )
                definition = SERVICE_CATALOG[service_type]
                order = ServiceOrder(
                    user_id=user.id,
                    service_type=service_type.value,
                    status=ServiceOrderStatus.QUEUED,
                    title=_short_title(payload[definition.title_field]),
                    request_payload=payload,
                    normalized_request=payload,
                    request_hash=request_hash,
                    generation_metadata={},
                    result_metadata={},
                    queued_at=now,
                    idempotency_key=idempotency_key,
                )
                session.add(order)
                await session.flush()
                await OutboxService().enqueue(
                    session,
                    event_type=profile.requested_event,
                    aggregate_type="service_order",
                    aggregate_id=str(order.id),
                    idempotency_key=f"service-{service_type.value}:{order.id}:1",
                    payload={"service_order_id": order.id},
                )
                return order
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(ServiceOrder)
                .join(User, User.id == ServiceOrder.user_id)
                .where(
                    ServiceOrder.idempotency_key == idempotency_key,
                    User.telegram_user_id == telegram_user_id,
                )
            )
            if existing is None:
                raise
            return self._validated_duplicate(
                existing,
                user_id=existing.user_id,
                service_type=service_type,
                request_hash=request_hash,
            )

    @staticmethod
    def _validated_duplicate(
        existing: ServiceOrder,
        *,
        user_id: int,
        service_type: ServiceType,
        request_hash: str,
    ) -> ServiceOrder:
        if (
            existing.user_id != user_id
            or existing.service_type != service_type.value
            or existing.request_hash != request_hash
        ):
            raise ValueError("idempotency key was already used for another request")
        return existing

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        error_code: str,
        error_message: str,
    ) -> None:
        async with session.begin():
            order = await session.get(ServiceOrder, order_id, with_for_update=True)
            if order is None:
                raise LookupError("service order not found")
            if order.status == ServiceOrderStatus.COMPLETED:
                raise ValueError("completed service order cannot fail")
            order.status = ServiceOrderStatus.FAILED
            order.error_code = error_code[:64]
            order.error_message = error_message[:1000]
            order.retry_count += 1
