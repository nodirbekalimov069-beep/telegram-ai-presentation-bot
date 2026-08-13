from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.bot.handlers import menu as menu_handlers
from apps.bot.handlers.menu import _send_orders
from apps.bot.localization import Localizer
from core.config import Settings
from core.database.base import Base
import core.database.models  # noqa: F401
from core.outbox.models import OutboxEvent
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.service_orders.essay_delivery import TelegramCourseWorkDelivery
from modules.service_orders.catalog import ServiceType
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.models import User
from modules.users.service import UserService


@pytest.fixture
async def service_order_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'service-orders.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=777,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_preview_order_creation_is_idempotent_and_server_validated(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    request = {
        "topic": "AI xavfsizligi",
        "language": "uz",
        "length": "medium",
        "requirements": "Manbalar ko‘rsatilsin",
    }
    async with service_order_db() as session:
        first = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.ESSAY,
            request_payload=request,
            idempotency_key="svc-test-0001",
        )
    async with service_order_db() as session:
        duplicate = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.ESSAY,
            request_payload=request,
            idempotency_key="svc-test-0001",
        )
        count = await session.scalar(select(func.count(ServiceOrder.id)))

    assert duplicate.id == first.id
    assert count == 1
    assert first.status == ServiceOrderStatus.PREVIEW
    assert first.title == "AI xavfsizligi"
    assert first.result_metadata == {}
    assert first.error_message is None
    assert first.retry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", [ServiceType.TRANSLATE, ServiceType.DOCUMENT])
async def test_closed_successor_cannot_create_direct_preview_order(
    service_order_db, service_type: ServiceType
) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    payload = (
        {"text": "Sinov uchun yetarlicha uzun matn.", "target_language": "en"}
        if service_type is ServiceType.TRANSLATE
        else {"document_type": "application", "details": "Universitet uchun"}
    )
    async with service_order_db() as session:
        with pytest.raises(PermissionError, match="preview service is closed"):
            await service.create_preview_order(
                session,
                telegram_user_id=777,
                service_type=service_type,
                request_payload=payload,
                idempotency_key=f"closed-{service_type.value}-0002",
            )
        count = await session.scalar(select(func.count(ServiceOrder.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_document_order_is_idempotent_with_one_transactional_outbox_event(
    service_order_db,
) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    request = {
        "document_type": "application",
        "details": (
            "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
            "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
        ),
        "language": "uz",
    }
    async with service_order_db() as session:
        first = await service.create_document_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="document-test-0001",
        )
    async with service_order_db() as session:
        duplicate = await service.create_document_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="document-test-0001",
        )
        events = (await session.scalars(select(OutboxEvent))).all()

    assert duplicate.id == first.id
    assert first.service_type == ServiceType.DOCUMENT.value
    assert first.status == ServiceOrderStatus.QUEUED
    assert first.title == "application"
    assert len(events) == 1
    assert events[0].event_type == "service.document.requested"
    assert events[0].payload == {"service_order_id": first.id}


@pytest.mark.asyncio
async def test_course_work_automatic_section_label_is_canonical_before_order(
    service_order_db,
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_course_work_order(
            session,
            telegram_user_id=777,
            request_payload={
                "subject": "Biologiya",
                "topic": "Hujayra tuzilishi",
                "sections": "Bo‘limlarni o‘zing top",
                "length": "short",
                "language": "uz",
                "requirements": "",
            },
            idempotency_key="course-auto-sections",
        )
        events = list(await session.scalars(select(OutboxEvent)))

    assert order.normalized_request["sections"] == "__automatic_sections__"
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sections",
    [
        "Faqat bitta bo‘lim",
        "Kirish; Kirish",
        ";".join(f"Bo‘lim {index}" for index in range(13)),
    ],
)
async def test_invalid_explicit_course_sections_have_zero_durable_delta(
    service_order_db, sections: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    provider = AsyncMock()
    delivery = AsyncMock()
    monkeypatch.setattr(OpenAICompatibleProvider, "generate_course_work", provider)
    monkeypatch.setattr(TelegramCourseWorkDelivery, "deliver", delivery)

    async def counts(session: AsyncSession) -> tuple[int, int, int, int]:
        return (
            int(await session.scalar(select(func.count(ServiceOrder.id))) or 0),
            int(await session.scalar(select(func.count(OutboxEvent.id))) or 0),
            int(
                await session.scalar(
                    select(func.count(ServiceOrder.id)).where(
                        or_(
                            ServiceOrder.docx_storage_key.is_not(None),
                            ServiceOrder.pdf_storage_key.is_not(None),
                        )
                    )
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count(ServiceOrder.id)).where(
                        or_(
                            ServiceOrder.delivery_status != "PENDING",
                            ServiceOrder.delivery_attempts != 0,
                            ServiceOrder.docx_telegram_message_id.is_not(None),
                            ServiceOrder.pdf_telegram_message_id.is_not(None),
                        )
                    )
                )
                or 0
            ),
        )

    async with service_order_db() as session:
        before = await counts(session)
        with pytest.raises(ValueError, match="sections"):
            await ServiceOrderService().create_course_work_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "subject": "Biologiya",
                    "topic": "Hujayra tuzilishi",
                    "sections": sections,
                    "length": "short",
                    "language": "uz",
                    "requirements": "",
                },
                idempotency_key="course-invalid-sections",
            )
        after = await counts(session)

    assert tuple(after[index] - before[index] for index in range(4)) == (0, 0, 0, 0)
    provider.assert_not_awaited()
    delivery.assert_not_awaited()


@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector met Dr. This document was approved by the committee after review."),
        ("en", "The rector met Prof. This document was approved by the committee after review."),
        ("uz", "Rektor dots. Bu hujjat komissiya tomonidan tasdiqlandi."),
        ("ru", "Директор встретил проф. Этот документ утвердила комиссия."),
        ("en", "The rector met Ph.D. This document was approved by the committee."),
        ("en", "The rector met M.D. This document was approved by the committee."),
        ("en", "The rector met B.Sc. This document was approved by the committee."),
        ("en", "The rector met M.Sc. This document was approved by the committee."),
    ],
)
async def test_document_context_free_abbreviation_boundary_creates_order(
    service_order_db, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=777,
            request_payload={
                "document_type": "official_letter",
                "details": details,
                "language": language,
            },
            idempotency_key="document-abbreviation-boundary",
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order.normalized_request["details"] == details
    assert order_count == 1
    assert outbox_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "document_type": "official_letter",
            "details": "Please add the rector signature and official seal to make this approved.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor tasdiqladi. Imzo: A. Rasmiy muhr qo‘yildi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Утверждено директором. Подпись: А. Официальная печать поставлена.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The rector approves this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The director authorizes this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor ushbu hujjatni tasdiqladi va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Ushbu hujjatni tasdiqladi rektor va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Директор утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Директор утверждает этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ утвердил директор для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ утверждает директор для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "application",
            "details": ["Toshkent davlat universiteti rektoriga."] * 2,
            "language": "uz",
        },
        {
            "document_type": "application",
            "details": {"text": "Toshkent davlat universiteti rektoriga."},
            "language": "uz",
        },
        {
            "document_type": ["application"],
            "details": "Toshkent davlat universiteti rektoriga ta’til arizasi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, after reviewing all submitted materials and consulting "
            "the university administrative committee, approved this document.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was approved after full review of all submitted "
            "materials and consultation with the university administrative committee by "
            "the rector.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor barcha taqdim etilgan materiallarni batafsil ko‘rib chiqib "
            "va universitet ma’muriy kengashi bilan maslahatlashib, hujjatni tasdiqladi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Hujjatni tasdiqladi barcha taqdim etilgan materiallarni ko‘rib "
            "chiqish va universitet ma’muriy kengashi bilan maslahatlashishdan keyin "
            "rektor.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Директор после подробного рассмотрения всех представленных "
            "материалов и консультации с административным советом университета утвердил "
            "этот документ.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ утвердил после подробного рассмотрения всех "
            "представленных материалов и консультации с административным советом "
            "университета директор.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, Ph.D. approved this document after reviewing the "
            "submitted materials.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Ｔｈｅ ｒｅｃｔｏｒ ａｐｐｒｏｖｅｄ ｔｈｉｓ ｄｏｃｕｍｅｎｔ "
            "ｆｏｒ ｉｍｍｅｄｉａｔｅ ｕｓｅ．",
            "language": "en",
        },
        {"document_type": "other", "details": "\ufb03" * 4000, "language": "en"},
        {"document_type": "other", "details": "A\u030a" * 10, "language": "en"},
        {
            "document_type": "official_letter",
            "details": "The rector,\nafter reviewing all submitted materials, approved this document.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor barcha materiallarni ko‘rib chiqib,\nhujjatni tasdiqladi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Директор после подробного рассмотрения материалов\nутвердил этот документ.",
            "language": "ru",
        },
        {"document_type": "other", "details": " " * 19 + "a", "language": "en"},
        {
            "document_type": "official_letter",
            "details": "The dean approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Dekan ushbu hujjatni tasdiqladi va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Декан утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The dean endorsed this document as official and valid.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was formally endorsed as valid by the dean.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Dekan ushbu hujjatni ma’qulladi va rasmiy kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Ushbu hujjatni rasmiy kuchga kiritish uchun dekan ma’qullagan.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Декан согласовал этот документ и придал ему официальную силу.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ был согласован деканом и получил официальную силу.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The dеan approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Dеkan ushbu hujjatni tasdiqladi va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Дeкан утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The chancellor approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was authorized by the vice-chancellor after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The dіrector approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The dean apprоved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Mudir ushbu hujjatni tasdiqladi va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Ushbu hujjat mudir tomonidan tasdiqlangan va kuchga kiritilgan.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Prorektor ushbu hujjatni ma’qullagan va rasmiy deb e’lon qilgan.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Mudir ushbu hujjatni tasdіqladi va darhol kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Проректор утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ был утвержден вице-ректором после проверки.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Канцлер согласовал этот документ и придал ему официальную силу.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Проректор утвeрдил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, A. Smith, approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, A. B. Smith, approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor A. Karimov ushbu hujjatni tasdiqladi va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor A. B. Karimov ushbu hujjatni tasdiqladi va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Директор И. Иванов утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Директор И. И. Иванов утвердил этот документ для применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, Rev. Smith, approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was approved by Rev. Smith, the rector after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor, dots. Karimov, ushbu hujjatni tasdiqladi va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Ushbu hujjatni tasdiqladi dots. Karimov, rektor, batafsil ko‘rib chiqib.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Директор, проф. Иванов, утвердил этот документ для применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ утвердил проф. Иванов, директор, после проверки.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The dіrесtor approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was аррrоved by the director after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Dіrекtor ushbu hujjatni tasdiqladi va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Ushbu hujjatni tаѕdіqladi direktor va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Диpeктoр утвердил этот документ для немедленного применения.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ yтвepдил директор после проверки.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, Dr. Smith, approved this document for immediate use.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was approved by Prof. Smith, the rector after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "Rektor, dots. Karimov, ushbu hujjatni tasdiqladi va kuchga kiritdi.",
            "language": "uz",
        },
        {
            "document_type": "official_letter",
            "details": "Этот документ утвердил проф. Иванов, директор, после проверки.",
            "language": "ru",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, Alex Smith, Ph.D., approved this document after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was approved by Alex Smith, M.D., the rector after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "The rector, Alex Smith, B.Sc., approved this document after review.",
            "language": "en",
        },
        {
            "document_type": "official_letter",
            "details": "This document was approved by Alex Smith, M.Sc., the rector after review.",
            "language": "en",
        },
    ],
)
async def test_document_unsafe_or_malformed_input_creates_no_durable_state(
    service_order_db, monkeypatch, payload: dict[str, object]
) -> None:  # type: ignore[no-untyped-def]
    provider_call = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(MockAIProvider, "generate_document", provider_call)
    async with service_order_db() as session:
        with pytest.raises(ValueError):
            await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=777,
                request_payload=payload,
                idempotency_key="document-rejected-before-order",
            )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        artifact_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_storage_key.is_not(None))
                | (ServiceOrder.pdf_storage_key.is_not(None))
            )
        )
        delivery_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_telegram_message_id.is_not(None))
                | (ServiceOrder.pdf_telegram_message_id.is_not(None))
                | (ServiceOrder.delivered_at.is_not(None))
            )
        )

    assert order_count == 0
    assert outbox_count == 0
    assert artifact_count == 0
    assert delivery_count == 0
    assert provider_call.await_count == 0


@pytest.mark.asyncio
async def test_blocked_or_unknown_user_cannot_create_service_order(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    payload = {
        "text": "Bu jamoa uchun yetarli va tushunarli sinov matni hisoblanadi.",
        "mode": "simple",
    }
    async with service_order_db() as session:
        with pytest.raises(PermissionError):
            await service.create_preview_order(
                session,
                telegram_user_id=999,
                service_type=ServiceType.REWRITE,
                request_payload=payload,
                idempotency_key="svc-test-0003",
            )
    async with service_order_db() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_user_id == 777))
        assert user is not None
        user.is_blocked = True
    async with service_order_db() as session:
        with pytest.raises(PermissionError):
            await service.create_preview_order(
                session,
                telegram_user_id=777,
                service_type=ServiceType.REWRITE,
                request_payload=payload,
                idempotency_key="svc-test-0004",
            )


@pytest.mark.asyncio
async def test_failure_metadata_and_retry_counter_are_persisted(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    async with service_order_db() as session:
        order = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.ESSAY,
            request_payload={
                "topic": "Failure persistence sinovi",
                "language": "uz",
                "length": "short",
                "requirements": "",
            },
            idempotency_key="svc-test-0005",
        )
    async with service_order_db() as session:
        await service.mark_failed(
            session,
            order_id=order.id,
            error_code="NOT_IMPLEMENTED",
            error_message="Generation engine is unavailable",
        )
    async with service_order_db() as session:
        saved = await session.get(ServiceOrder, order.id)

    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "NOT_IMPLEMENTED"
    assert saved.error_message == "Generation engine is unavailable"
    assert saved.retry_count == 1


@pytest.mark.asyncio
async def test_universal_orders_use_database_pagination_without_a_global_cap(
    service_order_db,
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        now = datetime.now(UTC)
        async with session.begin():
            user = await session.scalar(select(User).where(User.telegram_user_id == 777))
            assert user is not None
            session.add_all(
                [
                    ServiceOrder(
                        user_id=user.id,
                        service_type=ServiceType.ESSAY.value,
                        status=ServiceOrderStatus.PREVIEW.value,
                        title=f"topic-{index}",
                        request_payload={"topic": f"topic-{index}"},
                        request_hash=f"{index:064d}",
                        result_metadata={},
                        retry_count=0,
                        idempotency_key=f"pagination-{index}",
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                    for index in range(13)
                ]
            )
        message = SimpleNamespace(answer=AsyncMock())
        await _send_orders(
            message,
            session,
            user,
            Localizer.load_default(),
            page=999,
        )

    text = message.answer.await_args.args[0]
    assert "Sahifa 3/3" in text
    assert text.count("• Referat") == 3


@pytest.mark.asyncio
async def test_essay_order_is_queued_with_one_transactional_outbox_event(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    request = {
        "topic": "O‘zbekiston iqtisodiyoti",
        "language": "uz",
        "length": "medium",
        "requirements": "Hududiy misollar keltirilsin",
    }
    async with service_order_db() as session:
        first = await service.create_essay_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="essay-test-0001",
        )
    async with service_order_db() as session:
        duplicate = await service.create_essay_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="essay-test-0001",
        )
        events = (await session.scalars(select(OutboxEvent))).all()

    assert duplicate.id == first.id
    assert first.status == ServiceOrderStatus.QUEUED
    assert first.generation_attempts == 0
    assert first.queued_at is not None
    assert first.docx_storage_key is None
    assert len(events) == 1
    assert events[0].event_type == "service.essay.requested"
    assert events[0].payload == {"service_order_id": first.id}


@pytest.mark.asyncio
async def test_independent_work_order_is_idempotent_with_one_outbox_event(
    service_order_db,
) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    request = {
        "subject": "Informatika",
        "topic": "Algoritmlarning amaliy ahamiyati",
        "language": "uz",
        "length": "short",
        "requirements": "Xulosada algoritmik fikrlash iborasini ishlat.",
    }
    async with service_order_db() as session:
        first = await service.create_independent_work_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="independent-test-0001",
        )
    async with service_order_db() as session:
        duplicate = await service.create_independent_work_order(
            session,
            telegram_user_id=777,
            request_payload=request,
            idempotency_key="independent-test-0001",
        )
        events = (await session.scalars(select(OutboxEvent))).all()

    assert duplicate.id == first.id
    assert first.service_type == ServiceType.INDEPENDENT_WORK.value
    assert first.status == ServiceOrderStatus.QUEUED
    assert first.title == "Algoritmlarning amaliy ahamiyati"
    assert first.normalized_request["subject"] == "Informatika"
    assert len(events) == 1
    assert events[0].event_type == "service.independent.requested"
    assert events[0].payload == {"service_order_id": first.id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_type", "artifact_stem"),
    [
        (ServiceType.ESSAY, "referat"),
        (ServiceType.INDEPENDENT_WORK, "mustaqil_ish"),
        (ServiceType.COURSE_WORK, "kurs_ishi"),
    ],
)
async def test_service_artifact_retrieval_rejects_another_users_order(
    service_order_db,
    monkeypatch: pytest.MonkeyPatch,
    service_type: ServiceType,
    artifact_stem: str,
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        other = await UserService().register(
            session,
            telegram_user_id=778,
            username="other",
            first_name="Other",
            last_name=None,
            language_code="uz",
        )
        async with session.begin():
            foreign_order = ServiceOrder(
                user_id=other.id,
                service_type=service_type.value,
                status=ServiceOrderStatus.ARTIFACTS_READY,
                title="Boshqa foydalanuvchi",
                request_payload={"topic": "Boshqa"},
                normalized_request={"topic": "Boshqa"},
                request_hash="f" * 64,
                generation_metadata={},
                result_metadata={},
                idempotency_key=f"foreign-artifact-order-{service_type.value}",
                docx_storage_key=f"users/2/service-orders/9/{artifact_stem}_9.docx",
            )
            session.add(foreign_order)
        monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
        build_storage = AsyncMock()
        monkeypatch.setattr(menu_handlers, "build_storage", build_storage)
        callback = SimpleNamespace(
            data=f"sva:{foreign_order.id}",
            message=SimpleNamespace(answer_document=AsyncMock(), answer=AsyncMock()),
            from_user=SimpleNamespace(id=777),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())

        await menu_handlers.retrieve_service_artifacts(
            callback,
            state,  # type: ignore[arg-type]
            session,
            Localizer.load_default(),
            Settings(app_env="test"),
        )

    callback.answer.assert_awaited_once_with("Fayl hozir mavjud emas.", show_alert=True)
    build_storage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_type", "artifact_stem", "service_caption"),
    [
        (ServiceType.ESSAY, "referat", "Referat"),
        (ServiceType.INDEPENDENT_WORK, "mustaqil_ish", "Mustaqil ish"),
        (ServiceType.COURSE_WORK, "kurs_ishi", "Kurs ishi"),
    ],
)
async def test_service_artifact_retrieval_sends_owned_docx_and_pdf(
    service_order_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service_type: ServiceType,
    artifact_stem: str,
    service_caption: str,
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        service = ServiceOrderService()
        if service_type is ServiceType.ESSAY:
            order = await service.create_essay_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "topic": "Owned artifact",
                    "language": "uz",
                    "length": "short",
                    "requirements": "",
                },
                idempotency_key="essay-owned-artifact",
            )
        elif service_type is ServiceType.INDEPENDENT_WORK:
            order = await service.create_independent_work_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "subject": "Informatika",
                    "topic": "Owned artifact",
                    "language": "uz",
                    "length": "short",
                    "requirements": "",
                },
                idempotency_key="independent-owned-artifact",
            )
        else:
            order = await service.create_course_work_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "subject": "Informatika",
                    "topic": "Owned artifact",
                    "sections": "Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
                    "language": "uz",
                    "length": "short",
                    "requirements": "",
                },
                idempotency_key="course-owned-artifact",
            )
        order.docx_storage_key = f"users/1/service-orders/{order.id}/{artifact_stem}.docx"
        order.pdf_storage_key = f"users/1/service-orders/{order.id}/{artifact_stem}.pdf"
        await session.commit()

        docx = tmp_path / f"{artifact_stem}.docx"
        pdf = tmp_path / f"{artifact_stem}.pdf"
        docx.write_bytes(b"docx")
        pdf.write_bytes(b"%PDF-1.7")
        storage = SimpleNamespace(download_for_user=AsyncMock(side_effect=[docx, pdf]))
        monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
        monkeypatch.setattr(menu_handlers, "build_storage", lambda settings: storage)
        message = SimpleNamespace(answer_document=AsyncMock(), answer=AsyncMock())
        callback = SimpleNamespace(
            data=f"sva:{order.id}",
            from_user=SimpleNamespace(id=777),
            message=message,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())

        await menu_handlers.retrieve_service_artifacts(
            callback,
            state,  # type: ignore[arg-type]
            session,
            Localizer.load_default(),
            Settings(
                app_env="test",
                generation_work_root=tmp_path / "generation",
                max_upload_bytes=20_000_000,
            ),
        )

    assert message.answer_document.await_count == 2
    assert storage.download_for_user.await_count == 2
    filenames = [call.args[0].filename for call in message.answer_document.await_args_list]
    captions = [call.kwargs["caption"] for call in message.answer_document.await_args_list]
    assert filenames == [
        f"{artifact_stem}_{order.id}.docx",
        f"{artifact_stem}_{order.id}.pdf",
    ]
    assert captions == [
        f"📎 {service_caption} — DOCX",
        f"📎 {service_caption} — PDF",
    ]
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_service_artifact_retrieval_rejects_int64_overflow_callback(
    service_order_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(menu_handlers, "Message", SimpleNamespace)
    build_storage = Mock()
    monkeypatch.setattr(menu_handlers, "build_storage", build_storage)
    callback = SimpleNamespace(
        data="sva:" + "9" * 59,
        message=SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock()),
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    async with service_order_db() as session:
        await menu_handlers.retrieve_service_artifacts(
            callback,
            state,  # type: ignore[arg-type]
            session,
            Localizer.load_default(),
            Settings(app_env="test"),
        )

    callback.answer.assert_awaited_once_with()
    callback.message.answer_document.assert_not_awaited()
    build_storage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector Dr. Smith approved this document after review."),
        ("en", "The rector met Dr. Smith approved this document after review."),
        ("en", "This document was approved by Prof. Alice Smith the rector after review."),
        ("uz", "Rektor dots. Ali Karimov ushbu hujjatni tasdiqladi va kuchga kiritdi."),
        ("uz", "Ushbu hujjatni tasdiqladi dots. Ali Karimov rektor batafsil ko‘rib chiqib."),
        ("ru", "Директор проф. Иван Иванов утвердил этот документ после проверки."),
        ("ru", "Этот документ утвердил проф. Иван Иванов директор после проверки."),
    ],
)
async def test_document_bounded_title_name_rejects_with_zero_state(
    service_order_db, monkeypatch, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    provider_call = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(MockAIProvider, "generate_document", provider_call)
    async with service_order_db() as session:
        with pytest.raises(ValueError):
            await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "document_type": "official_letter",
                    "details": details,
                    "language": language,
                },
                idempotency_key="document-bounded-title-name-rejected",
            )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        artifact_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_storage_key.is_not(None))
                | (ServiceOrder.pdf_storage_key.is_not(None))
            )
        )
        delivery_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_telegram_message_id.is_not(None))
                | (ServiceOrder.pdf_telegram_message_id.is_not(None))
                | (ServiceOrder.delivered_at.is_not(None))
            )
        )
    assert order_count == outbox_count == artifact_count == delivery_count == 0
    assert provider_call.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector met Dr. This document was approved by the committee after review."),
        ("en", "The rector met Prof. That document was approved by the committee after review."),
        ("uz", "Rektor dots. Ushbu hujjat komissiya tomonidan tasdiqlandi."),
        ("ru", "Директор встретил проф. Это документ утвердила комиссия."),
        ("ru", "Директор встретил проф. Этот документ утвердила комиссия."),
    ],
)
async def test_document_sentence_opener_after_title_creates_isolated_order(
    service_order_db, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=777,
            request_payload={
                "document_type": "official_letter",
                "details": details,
                "language": language,
            },
            idempotency_key="document-title-sentence-opener",
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order.normalized_request["details"] == details
    assert order_count == outbox_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The dжrector approved the meeting agenda after committee review."),
        ("en", "The dжrесtor approved the meeting agenda after committee review."),
        ("uz", "Tжsdiq jarayoni direktor ishtirok etgan yig‘ilishda muhokama qilindi."),
        ("ru", "Директор уxвердил только расписание планового заседания комиссии."),
    ],
)
async def test_document_unrelated_cross_script_control_creates_isolated_order(
    service_order_db, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=777,
            request_payload={
                "document_type": "official_letter",
                "details": details,
                "language": language,
            },
            idempotency_key="document-unrelated-confusable-control",
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order.normalized_request["details"] == details
    assert order_count == outbox_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector said: “Done.”))) This document was approved by committee."),
        ("en", "This document was approved by committee. «Done.»]))) The rector attended later."),
        ("uz", "Rektor aytdi: «Tayyor.»]))) Bu hujjat komissiya tomonidan tasdiqlandi."),
        ("uz", "Hujjat komissiya tomonidan tasdiqlandi. «Tayyor.»]))) Rektor keyin qatnashdi."),
        ("ru", "Директор сказал: «Готово.»]))) Этот документ утвердила комиссия."),
        ("ru", "Документ утвердила комиссия. «Готово.»]))) Директор пришёл позже."),
    ],
)
async def test_document_many_closing_delimiters_create_isolated_order(
    service_order_db, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=777,
            request_payload={
                "document_type": "official_letter",
                "details": details,
                "language": language,
            },
            idempotency_key="document-many-closing-delimiters",
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order.normalized_request["details"] == details
    assert order_count == outbox_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector, D.Phil. approved this document after review."),
        ("en", "This document was approved by Alex Smith, Ed.D., the rector after review."),
        ("uz", "Rektor, LL.M. ushbu hujjatni tasdiqladi va kuchga kiritdi."),
        ("uz", "Ushbu hujjatni tasdiqladi Ali Karimov, MBA., rektor batafsil ko‘rib chiqib."),
        ("ru", "Директор, D.Phil. утвердил этот документ после проверки."),
        ("ru", "Этот документ утвердил Иван Иванов, Ed.D., директор после проверки."),
    ],
)
async def test_document_bounded_degree_authority_rejects_with_zero_state(
    service_order_db, monkeypatch, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    provider_call = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(MockAIProvider, "generate_document", provider_call)
    async with service_order_db() as session:
        with pytest.raises(ValueError):
            await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "document_type": "official_letter",
                    "details": details,
                    "language": language,
                },
                idempotency_key="document-bounded-degree-rejected",
            )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        artifact_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_storage_key.is_not(None))
                | (ServiceOrder.pdf_storage_key.is_not(None))
            )
        )
        delivery_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_telegram_message_id.is_not(None))
                | (ServiceOrder.pdf_telegram_message_id.is_not(None))
                | (ServiceOrder.delivered_at.is_not(None))
            )
        )
    assert order_count == outbox_count == artifact_count == delivery_count == 0
    assert provider_call.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The rector A. B. C. D. Smith approved this document after review."),
        ("uz", "Rektor A. B. C. D. E. F. G. H. Karimov ushbu hujjatni tasdiqladi."),
        ("ru", "Директор И. И. И. И. И. И. И. И. Иванов утвердил этот документ."),
        ("en", "The rector A. B. C. D. E. F. G. H. I. Smith approved this document."),
    ],
)
async def test_document_initial_run_authority_rejects_with_zero_state(
    service_order_db, monkeypatch, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    provider_call = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(MockAIProvider, "generate_document", provider_call)
    async with service_order_db() as session:
        with pytest.raises(ValueError):
            await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "document_type": "official_letter",
                    "details": details,
                    "language": language,
                },
                idempotency_key="document-initial-run-rejected",
            )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        artifact_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_storage_key.is_not(None))
                | (ServiceOrder.pdf_storage_key.is_not(None))
            )
        )
        delivery_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_telegram_message_id.is_not(None))
                | (ServiceOrder.pdf_telegram_message_id.is_not(None))
                | (ServiceOrder.delivered_at.is_not(None))
            )
        )
    assert order_count == outbox_count == artifact_count == delivery_count == 0
    assert provider_call.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The dіrectorж approved the meeting agenda after committee review."),
        ("uz", "Direktor tаѕdіqж jarayonini yig‘ilishda batafsil muhokama qildi."),
    ],
)
async def test_document_unrelated_confusable_suffix_creates_isolated_order(
    service_order_db, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        order = await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=777,
            request_payload={
                "document_type": "official_letter",
                "details": details,
                "language": language,
            },
            idempotency_key="document-unrelated-confusable-suffix",
        )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order.normalized_request["details"] == details
    assert order_count == outbox_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "details"),
    [
        ("en", "The dіrectors approved this document for immediate use."),
        ("uz", "Dіrекtorlar ushbu hujjatni tasdiqladi va kuchga kiritdi."),
        ("ru", "Диpeктoры утвердили этот документ для немедленного применения."),
    ],
)
async def test_document_confusable_role_morphology_rejects_with_zero_state(
    service_order_db, monkeypatch, language: str, details: str
) -> None:  # type: ignore[no-untyped-def]
    provider_call = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(MockAIProvider, "generate_document", provider_call)
    async with service_order_db() as session:
        with pytest.raises(ValueError):
            await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=777,
                request_payload={
                    "document_type": "official_letter",
                    "details": details,
                    "language": language,
                },
                idempotency_key="document-confusable-role-morphology-rejected",
            )
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        artifact_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_storage_key.is_not(None))
                | (ServiceOrder.pdf_storage_key.is_not(None))
            )
        )
        delivery_count = await session.scalar(
            select(func.count(ServiceOrder.id)).where(
                (ServiceOrder.docx_telegram_message_id.is_not(None))
                | (ServiceOrder.pdf_telegram_message_id.is_not(None))
                | (ServiceOrder.delivered_at.is_not(None))
            )
        )
    assert order_count == outbox_count == artifact_count == delivery_count == 0
    assert provider_call.await_count == 0
