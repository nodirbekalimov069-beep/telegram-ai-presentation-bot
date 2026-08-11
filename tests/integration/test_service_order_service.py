from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.bot.handlers import menu as menu_handlers
from apps.bot.handlers.menu import _send_orders
from apps.bot.localization import Localizer
from core.config import Settings
from core.database.base import Base
import core.database.models  # noqa: F401
from core.outbox.models import OutboxEvent
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
