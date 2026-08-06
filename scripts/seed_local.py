"""Seed default plans into the configured local database."""

import asyncio

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from modules.plans.seed import seed_default_plans


async def main() -> None:
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await seed_default_plans(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
