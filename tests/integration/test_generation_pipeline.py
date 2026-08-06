from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database.base import Base
import core.database.models  # noqa: F401
from modules.files.storage import LocalStorage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.plans.seed import seed_default_plans
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import GenerationPipeline
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
            settings={"slide_count": 2},
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
    pipeline = GenerationPipeline(
        renderer=NodePresentationRenderer(project_root / "apps" / "presentation_engine"),
        pdf_converter=FakePdfConverter(),
        storage=storage,
        work_root=tmp_path / "work",
    )

    result = await pipeline.run(factory, presentation_id=presentation_id)

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
    await engine.dispose()
