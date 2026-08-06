from pathlib import Path

import core.database.models  # noqa: F401
import pytest
from pydantic import SecretStr
from apps.bot.quick_presentation import QuickPresentationService
from core.config import Settings
from core.database.base import Base
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.seed import seed_default_plans
from modules.presentations.engine import validate_pptx
from modules.presentations.models import Presentation
from modules.presentations.pdf import validate_pdf
from modules.users.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_quick_presentation_creates_paid_pptx_and_pdf_end_to_end(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quick.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        session.add(User(telegram_user_id=42, selected_language="uz"))

    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'quick.db'}",
        ai_provider="mock",
        payment_provider="mock",
        mock_payment_secret=SecretStr("test-secret"),
        storage_backend="local",
        local_storage_root=tmp_path / "storage",
        presentation_renderer=project_root / "apps/presentation_engine/render.mjs",
        generation_work_root=tmp_path / "work",
    )
    artifacts = await QuickPresentationService(settings).run(
        factory,
        telegram_user_id=42,
        text="Round Up dasturini yaratish amallari, rasmlar bilan 12 sahifali rangli slayd",
    )

    assert artifacts.pptx_path.is_file()
    assert artifacts.pdf_path.is_file()
    assert validate_pptx(artifacts.pptx_path, expected_slides=12).is_valid
    assert validate_pdf(artifacts.pdf_path, expected_pages=12).is_valid
    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.id == artifacts.order_id))
        presentation = await session.get(Presentation, artifacts.presentation_id)
    assert order is not None and order.status == OrderStatus.COMPLETED
    assert presentation is not None and presentation.status == OrderStatus.COMPLETED
    await engine.dispose()
