"""Run a real local PostgreSQL → PPTX → LibreOffice PDF artifact smoke test."""

import argparse
import asyncio
import json
import shutil
import time
import zipfile
from pathlib import Path

from sqlalchemy import select

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from modules.ai.contracts import Outline
from modules.files.storage import LocalStorage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.engine import NodePresentationRenderer, validate_pptx
from modules.presentations.generation import GenerationPipeline
from modules.presentations.models import GenerationJob, Presentation
from modules.presentations.pdf import LibreOfficeConverter, validate_pdf
from modules.users.models import User


_SYNTHETIC_FIXTURE_SOURCE = ["https://example.com/?dataset=synthetic-e2e-fixture"]


def _outline() -> Outline:
    slides: list[dict[str, object]] = [
        {
            "number": 1,
            "title": "Production pipeline E2E",
            "purpose": "Real renderer va converter tekshiruvi",
            "key_points": ["10 ta slayd", "Editable PPTX", "LibreOffice PDF"],
            "visual_type": "title",
            "layout": "cover",
            "sources": [],
        },
        {
            "number": 2,
            "title": "Test natijalari",
            "purpose": "Synthetic E2E fixture qiymatlarini ko'rsatish",
            "key_points": ["Qiymatlar faqat test fixture", "Fakt sifatida talqin qilinmaydi"],
            "visual_type": "chart",
            "layout": "chart_and_table",
            "sources": _SYNTHETIC_FIXTURE_SOURCE,
            "chart": {
                "type": "bar",
                "categories": ["Render", "Convert", "QA"],
                "series": [{"name": "Test balli", "values": [92, 88, 96]}],
            },
            "table": {
                "headers": ["Bosqich", "Test balli"],
                "rows": [["Render", 92], ["Convert", 88], ["QA", 96]],
            },
        },
        {
            "number": 3,
            "title": "Pipeline timeline",
            "purpose": "Asosiy generation ketma-ketligi",
            "key_points": ["Har bosqich tekshiriladi"],
            "visual_type": "timeline",
            "layout": "timeline",
            "sources": [],
            "special_layout": {
                "type": "timeline",
                "items": [
                    {"label": "Outline", "description": "Validated JSON"},
                    {"label": "PPTX", "description": "PptxGenJS"},
                    {"label": "PDF", "description": "LibreOffice"},
                    {"label": "QA", "description": "Structure"},
                ],
            },
        },
        {
            "number": 4,
            "title": "Generation process",
            "purpose": "Transactional process ko'rinishi",
            "key_points": ["Lease", "Fencing", "Idempotency"],
            "visual_type": "process",
            "layout": "process",
            "sources": [],
            "special_layout": {
                "type": "process",
                "items": [
                    {"label": "Queue"},
                    {"label": "Render"},
                    {"label": "Validate"},
                    {"label": "Store"},
                ],
            },
        },
        {
            "number": 5,
            "title": "Pipeline SWOT",
            "purpose": "Layout rendering smoke test",
            "key_points": ["To'rt kvadrant"],
            "visual_type": "swot",
            "layout": "swot",
            "sources": [],
            "special_layout": {
                "type": "swot",
                "strengths": ["Editable", "Idempotent"],
                "weaknesses": ["Local CPU"],
                "opportunities": ["Template growth"],
                "threats": ["Provider outage"],
            },
        },
        {
            "number": 6,
            "title": "Runtime komponentlari",
            "purpose": "Table rendering smoke test",
            "key_points": ["Container ichida real binarylar"],
            "visual_type": "table",
            "layout": "table",
            "sources": [],
            "table": {
                "headers": ["Komponent", "Vazifa"],
                "rows": [
                    ["Node.js", "PPTX renderer"],
                    ["LibreOffice", "PDF converter"],
                    ["PostgreSQL", "State"],
                ],
            },
        },
        {
            "number": 7,
            "title": "Retry trendi",
            "purpose": "Line chart rendering smoke test",
            "key_points": ["Synthetic E2E fixture"],
            "visual_type": "chart",
            "layout": "chart",
            "sources": _SYNTHETIC_FIXTURE_SOURCE,
            "chart": {
                "type": "line",
                "categories": ["1", "2", "3", "4"],
                "series": [{"name": "Test latency", "values": [1, 2, 4, 8]}],
            },
        },
        {
            "number": 8,
            "title": "Artifact ulushi",
            "purpose": "Pie chart rendering smoke test",
            "key_points": ["Synthetic E2E fixture"],
            "visual_type": "chart",
            "layout": "chart",
            "sources": _SYNTHETIC_FIXTURE_SOURCE,
            "chart": {
                "type": "pie",
                "categories": ["PPTX", "PDF"],
                "series": [{"name": "Artifact", "values": [1, 1]}],
            },
        },
        {
            "number": 9,
            "title": "QA mezonlari",
            "purpose": "Acceptance mezonlarini jamlash",
            "key_points": [
                "PPTX ZIP valid",
                "10 ta slide XML",
                "Editable chart workbook",
                "PDF 10 sahifa",
            ],
            "visual_type": "checklist",
            "layout": "title_and_content",
            "sources": [],
        },
        {
            "number": 10,
            "title": "Xulosa",
            "purpose": "Real E2E natijasini yakunlash",
            "key_points": ["Artifactlar structural QA'dan o'tishi shart"],
            "visual_type": "summary",
            "layout": "summary",
            "sources": [],
        },
    ]
    return Outline.model_validate({"title": "Production pipeline E2E", "slides": slides})


async def _run(output_dir: Path) -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    telegram_user_id = -(9_000_000_000_000 + time.time_ns() % 999_999_999)
    outline = _outline()

    try:
        async with session_factory() as session, session.begin():
            plan = await session.scalar(select(Plan).where(Plan.code == "medium"))
            if plan is None:
                raise RuntimeError("medium plan is not seeded")
            user = User(
                telegram_user_id=telegram_user_id,
                username="local_artifact_e2e",
                selected_language="uz",
            )
            session.add(user)
            await session.flush()
            order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=plan.price_uzs,
                currency="UZS",
                status=OrderStatus.QUEUED,
                topic=outline.title,
                settings={"slide_count": len(outline.slides), "e2e": True},
            )
            session.add(order)
            await session.flush()
            presentation = Presentation(
                order_id=order.id,
                user_id=user.id,
                title=outline.title,
                topic=outline.title,
                language="uz",
                direction="IT",
                audience="Engineering",
                slide_count=len(outline.slides),
                design_style="technology",
                outline=outline.model_dump(mode="json"),
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
            user_id = user.id

        storage = LocalStorage(settings.local_storage_root)
        pipeline = GenerationPipeline(
            renderer=NodePresentationRenderer(
                settings.presentation_renderer.parent,
                node_binary=settings.node_binary,
            ),
            pdf_converter=LibreOfficeConverter(binary=settings.libreoffice_binary),
            storage=storage,
            work_root=settings.generation_work_root,
        )
        result = await pipeline.run(session_factory, presentation_id=presentation_id)
        pptx_path = await storage.path_for_user(result.pptx_storage_key, user_id=user_id)
        pdf_path = await storage.path_for_user(result.pdf_storage_key, user_id=user_id)
        pptx_qa = validate_pptx(pptx_path, expected_slides=len(outline.slides))
        pdf_qa = validate_pdf(pdf_path, expected_pages=len(outline.slides))

        with zipfile.ZipFile(pptx_path) as archive:
            chart_count = sum(
                name.startswith("ppt/charts/chart") and name.endswith(".xml")
                for name in archive.namelist()
            )
            workbook_count = sum(
                name.startswith("ppt/embeddings/") and name.endswith(".xlsx")
                for name in archive.namelist()
            )
        if not pptx_qa.is_valid or not pdf_qa.is_valid:
            raise RuntimeError(f"artifact QA failed: {pptx_qa.issues}; {pdf_qa.issues}")
        if chart_count < 3 or workbook_count < 3:
            raise RuntimeError(
                f"expected at least 3 editable charts/workbooks, found {chart_count}/{workbook_count}"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        exported_pptx = output_dir / f"presentation-{presentation_id}.pptx"
        exported_pdf = output_dir / f"presentation-{presentation_id}.pdf"
        shutil.copyfile(pptx_path, exported_pptx)
        shutil.copyfile(pdf_path, exported_pdf)
        return {
            "presentation_id": presentation_id,
            "database_status": "ARTIFACTS_READY",
            "pptx": {
                "path": str(exported_pptx.resolve()),
                "bytes": exported_pptx.stat().st_size,
                "slides": pptx_qa.slide_count,
                "editable_charts": chart_count,
                "embedded_workbooks": workbook_count,
            },
            "pdf": {
                "path": str(exported_pdf.resolve()),
                "bytes": exported_pdf.stat().st_size,
                "pages": pdf_qa.page_count,
            },
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("/app/e2e-output"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.output_dir)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
