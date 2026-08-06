"""Validate configured Telegram credentials without printing the token."""

import asyncio

from aiogram import Bot

from core.config import get_settings


async def main() -> None:
    bot = Bot(get_settings().telegram_bot_token.get_secret_value())
    try:
        identity = await bot.get_me()
        print(f"BOT_OK @{identity.username}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
