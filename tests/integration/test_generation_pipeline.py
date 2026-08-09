from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database.base import Base
import core.database.models  # noqa: F401
from modules.ai.contracts import ContentGenerationRequest, GeneratedContent
from modules.ai.providers.mock import MockAIProvider
from modules.files.storage import LocalStorage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.plans.seed import seed_default_plans
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import (
    GenerationPipeline,
    mark_terminal_generation_failure,
)
from modules.presentations.models import GenerationAttempt, GenerationJob, Presentation
from modules.users.models import User


class FakePdfConverter:
    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path:
        assert pptx_path.is_file()
        writer = PdfWriter()
        for _ in range(2):
            writer.add_blank_page(width=960, height=540)
        with output_pdf.open("wb") as handle:
            writer.write(handle)
        return output_pdf


class NeverRenderer:
    async def render(self, payload: dict[str, object], output_path: Path) -> Path:
        del payload, output_path
        raise AssertionError("cancelled generation must not invoke the renderer")


class FailingRenderer:
    async def render(self, payload: dict[str, object], output_path: Path) -> Path:
        del payload, output_path
        raise RuntimeError("transient renderer failure")


class RecordingContentProvider:
    def __init__(self) -> None:
        self.requests: list[ContentGenerationRequest] = []

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent:
        self.requests.append(request)
        return await MockAIProvider().generate_content(request)


@pytest.mark.asyncio
async def test_generation_pipeline_creates_and_stores_both_artifacts(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'generation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=42, selected_language="uz")
        session.add(user)
        await session.flush()
        plan = await session.scalar(select(Plan).where(Plan.code == "premium"))
        assert plan is not None
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.price_uzs,
            currency="UZS",
            status=OrderStatus.QUEUED,
            topic="AI va ta’lim",
            settings={
                "slide_count": 2,
                "purpose": "Talabalarga tushuntirish",
                "audience": "Universitet talabalari",
                "knowledge_level": "beginner",
                "additional_requirements": "Har slaydda amaliy misol bo‘lsin",
            },
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title="AI va ta’lim",
            topic="AI va ta’lim",
            language="uz",
            direction="Ta’lim",
            audience="Talabalar",
            slide_count=2,
            design_style="ai",
            outline={
                "title": "AI va ta’lim",
                "slides": [
                    {
                        "number": 1,
                        "title": "Kirish",
                        "purpose": "Kontekst",
                        "key_points": ["AI imkoniyatlari", "Ta’lim ehtiyojlari"],
                        "visual_type": "icon",
                        "layout": "title_and_content",
                        "sources": [],
                    },
                    {
                        "number": 2,
                        "title": "Xulosa",
                        "purpose": "Yakunlash",
                        "key_points": ["Mas’uliyat", "Natijani o‘lchash"],
                        "visual_type": "icon",
                        "layout": "summary",
                        "sources": [],
                    },
                ],
            },
            content={},
            status=OrderStatus.QUEUED,
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="QUEUED",
                progress=0,
                current_step="queued",
                retries=0,
                priority=plan.priority,
            )
        )
        presentation_id = presentation.id

    project_root = Path(__file__).resolve().parents[2]
    storage = LocalStorage(tmp_path / "storage")
    content_provider = RecordingContentProvider()
    pipeline = GenerationPipeline(
        renderer=NodePresentationRenderer(project_root / "apps" / "presentation_engine"),
        pdf_converter=FakePdfConverter(),
        storage=storage,
        work_root=tmp_path / "work",
        content_provider=content_provider,  # type: ignore[arg-type]
    )
    progress_updates: list[tuple[int, str]] = []

    async def record_progress(progress: int, current_step: str) -> None:
        progress_updates.append((progress, current_step))

    result = await pipeline.run(
        factory,
        presentation_id=presentation_id,
        progress_callback=record_progress,
    )
    assert content_provider.requests[0].additional_requirements == (
        "Har slaydda amaliy misol bo‘lsin"
    )

    async with factory() as session:
        saved = await session.get(Presentation, presentation_id)
        saved_order = await session.get(Order, saved.order_id) if saved else None
        saved_user = await session.get(User, saved.user_id) if saved else None
        job = await session.scalar(
            select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
        )
        attempt = await session.scalar(select(GenerationAttempt))
    assert result.completed
    assert saved is not None and saved.status == OrderStatus.COMPLETED
    assert saved_order is not None and saved_order.status == OrderStatus.COMPLETED
    assert saved_user is not None and saved_user.presentations_count == 1
    assert job is not None and job.progress == 90
    assert job.status == "ARTIFACTS_READY" and job.current_step == "delivery_pending"
    assert job.lease_token is None
    assert attempt is not None and attempt.status == "COMPLETED"
    assert saved.pptx_storage_key and await storage.exists(saved.pptx_storage_key)
    assert saved.pdf_storage_key and await storage.exists(saved.pdf_storage_key)
    assert progress_updates == [
        (10, "content_and_design"),
        (20, "generating_content"),
        (30, "rendering_pptx"),
        (60, "converting_pdf"),
        (90, "delivery_pending"),
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_generation_pipeline_noops_for_cancelled_order(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancelled-generation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=43, selected_language="uz")
        session.add(user)
        await session.flush()
        plan = await session.scalar(select(Plan).where(Plan.code == "medium"))
        assert plan is not None
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.price_uzs,
            currency="UZS",
            status=OrderStatus.CANCELLED,
            topic="Cancelled",
            settings={"slide_count": 10},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title=order.topic,
            topic=order.topic,
            language="uz",
            direction="Ta’lim",
            audience="Talabalar",
            slide_count=10,
            design_style="modern",
            outline={},
            content={},
            status=OrderStatus.CANCELLED,
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="CANCELLED",
                progress=0,
                current_step="cancelled",
            )
        )
        presentation_id = presentation.id

    pipeline = GenerationPipeline(
        renderer=NeverRenderer(),  # type: ignore[arg-type]
        pdf_converter=FakePdfConverter(),
        storage=LocalStorage(tmp_path / "storage"),
        work_root=tmp_path / "work",
    )
    result = await pipeline.run(factory, presentation_id=presentation_id)

    assert result.completed is False
    assert result.pptx_storage_key == ""
    assert result.pdf_storage_key == ""
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_failure", "aggregate_status", "job_status", "current_step"),
    [
        (False, OrderStatus.QUEUED, "RETRYING", "retrying"),
        (True, OrderStatus.FAILED, "FAILED", "failed"),
    ],
)
async def test_generation_failure_records_retrying_or_terminal_durable_state(
    tmp_path: Path,
    terminal_failure: bool,
    aggregate_status: OrderStatus,
    job_status: str,
    current_step: str,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retrying-generation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=44, selected_language="uz")
        session.add(user)
        await session.flush()
        plan = await session.scalar(select(Plan).where(Plan.code == "medium"))
        assert plan is not None
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.price_uzs,
            currency="UZS",
            status=OrderStatus.QUEUED,
            topic="Retry test",
            settings={"slide_count": 10},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title=order.topic,
            topic=order.topic,
            language="uz",
            direction="IT",
            audience="Talabalar",
            slide_count=10,
            design_style="technology",
            outline={
                "title": order.topic,
                "slides": [
                    {
                        "number": 1,
                        "title": "Retry",
                        "purpose": "Transient failure",
                        "key_points": ["Retry safely"],
                        "visual_type": "icon",
                        "layout": "title_and_content",
                        "sources": [],
                    }
                ],
            },
            content={},
            status=OrderStatus.QUEUED,
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="QUEUED",
                progress=0,
                current_step="queued",
                retries=0,
                priority=plan.priority,
            )
        )
        presentation_id = presentation.id

    pipeline = GenerationPipeline(
        renderer=FailingRenderer(),  # type: ignore[arg-type]
        pdf_converter=FakePdfConverter(),
        storage=LocalStorage(tmp_path / "storage"),
        work_root=tmp_path / "work",
    )
    with pytest.raises(RuntimeError, match="transient renderer failure"):
        await pipeline.run(
            factory,
            presentation_id=presentation_id,
            terminal_failure=terminal_failure,
        )

    async with factory() as session:
        saved_presentation = await session.get(Presentation, presentation_id)
        saved_order = (
            await session.get(Order, saved_presentation.order_id)
            if saved_presentation is not None
            else None
        )
        job = await session.scalar(
            select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
        )
        assert job is not None
        attempt = await session.scalar(
            select(GenerationAttempt).where(GenerationAttempt.generation_job_id == job.id)
        )

    assert saved_order is not None and saved_order.status == aggregate_status
    assert saved_presentation is not None and saved_presentation.status == aggregate_status
    assert job.status == job_status
    assert job.current_step == current_step and job.retries == 1
    assert job.lease_token is None and job.lease_expires_at is None
    assert attempt is not None and attempt.status == "FAILED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_unstarted_failure_marks_queued_aggregate_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal-preflight.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(
            telegram_user_id=997,
            first_name="Terminal",
            selected_language="uz",
        )
        plan = await session.scalar(select(Plan).where(Plan.code == "free_demo"))
        assert plan is not None
        session.add(user)
        await session.flush()
        order = Order(
            public_id="terminal-preflight-order",
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.QUEUED,
            amount=0,
            currency="UZS",
            topic="AI",
            settings={"slide_count": 1},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title="AI",
            topic="AI",
            direction="Technology",
            audience="Engineers",
            language="uz",
            slide_count=1,
            design_style="modern",
            status=OrderStatus.QUEUED,
            outline={"title": "AI", "slides": []},
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="QUEUED",
                progress=0,
                current_step="queued",
            )
        )
        presentation_id = presentation.id
        order_id = order.id

    changed = await mark_terminal_generation_failure(
        factory,
        presentation_id=presentation_id,
        error=RuntimeError("renderer setup failed"),
    )

    async with factory() as session:
        saved_order = await session.get(Order, order_id)
        saved_presentation = await session.get(Presentation, presentation_id)
        job = await session.scalar(
            select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
        )
    assert changed is True
    assert saved_order is not None and saved_order.status == OrderStatus.FAILED
    assert saved_presentation is not None and saved_presentation.status == OrderStatus.FAILED
    assert job is not None and job.status == "FAILED"
    assert job.current_step == "failed" and job.retries == 1
    await engine.dispose()
