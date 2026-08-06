"""Idempotent database seed for user-manageable tariff plans."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.plans.models import Plan

DEFAULT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "code": "free_demo",
        "name": "Free Demo",
        "price_uzs": 0,
        "minimum_slides": 1,
        "maximum_slides": 8,
        "priority": 1,
        "features": {
            "pptx": True,
            "pdf": True,
            "images": True,
            "charts": "basic",
            "watermark": True,
            "speaker_notes": False,
            "free_revisions": 0,
        },
    },
    {
        "code": "medium",
        "name": "Medium",
        "price_uzs": 3_000,
        "minimum_slides": 10,
        "maximum_slides": 15,
        "priority": 10,
        "features": {
            "pptx": True,
            "pdf": True,
            "images": True,
            "charts": "basic",
            "watermark": False,
            "speaker_notes": False,
            "free_revisions": 0,
        },
    },
    {
        "code": "premium",
        "name": "Premium",
        "price_uzs": 5_000,
        "minimum_slides": 15,
        "maximum_slides": 20,
        "priority": 50,
        "features": {
            "pptx": True,
            "pdf": True,
            "images": "premium",
            "charts": "editable",
            "infographics": True,
            "swot": True,
            "timeline": True,
            "sources_slide": True,
            "watermark": False,
            "speaker_notes": False,
            "free_revisions": 0,
        },
    },
    {
        "code": "premium_plus",
        "name": "Premium+",
        "price_uzs": 7_990,
        "minimum_slides": 20,
        "maximum_slides": 30,
        "priority": 100,
        "features": {
            "pptx": True,
            "pdf": True,
            "images": "premium",
            "charts": "editable_advanced",
            "custom_design": True,
            "storytelling": True,
            "speaker_notes": True,
            "sources_slide": True,
            "watermark": False,
            "free_revisions": 1,
            "priority_queue": True,
        },
    },
)


async def seed_default_plans(session: AsyncSession) -> None:
    async with session.begin():
        existing = {
            plan.code: plan
            for plan in (
                await session.scalars(
                    select(Plan).where(
                        Plan.code.in_([definition["code"] for definition in DEFAULT_PLANS])
                    )
                )
            ).all()
        }
        for definition in DEFAULT_PLANS:
            plan = existing.get(definition["code"])
            if plan is None:
                session.add(Plan(**definition, is_active=True))
                continue
            for key, value in definition.items():
                setattr(plan, key, value)
