from unittest.mock import AsyncMock

import pytest

from modules.presentations.progress import TelegramProgressReporter


@pytest.mark.asyncio
async def test_progress_reporter_edits_one_telegram_message_for_all_checkpoints() -> None:
    bot = AsyncMock()
    reporter = TelegramProgressReporter(
        bot=bot,
        chat_id=555,
        message_id=1234,
        language="uz",
    )

    await reporter(10, "content_and_design")
    await reporter(30, "rendering_pptx")
    await reporter(60, "converting_pdf")
    await reporter(90, "delivery_pending")
    await reporter(100, "delivered")

    assert bot.edit_message_text.await_count == 5
    assert {
        (call.kwargs["chat_id"], call.kwargs["message_id"])
        for call in bot.edit_message_text.await_args_list
    } == {(555, 1234)}
    texts = [call.kwargs["text"] for call in bot.edit_message_text.await_args_list]
    assert texts == [
        "10% · 📝 Slaydlar tuzilmoqda...",
        "30% · 📊 Grafik va jadvallar yaratilmoqda...",
        "60% · 📄 PDF tayyorlanmoqda...",
        "90% · 📤 Fayllar Telegram’ga yuborilmoqda...",
        "100% · ✅ Tayyor!",
    ]


@pytest.mark.asyncio
async def test_progress_reporter_shows_friendly_generation_failure() -> None:
    bot = AsyncMock()
    reporter = TelegramProgressReporter(
        bot=bot,
        chat_id=555,
        message_id=777,
        language="uz",
    )

    await reporter(0, "failed")

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=555,
        message_id=777,
        text="⚠️ Prezentatsiyani tayyorlashda xatolik yuz berdi.",
    )
