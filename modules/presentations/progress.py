"""Best-effort single-message Telegram generation progress updates."""

import logging
from dataclasses import dataclass
from typing import Protocol

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.presentations.models import GenerationJob, Presentation

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProgressDestination:
    generation_job_id: int
    chat_id: int
    message_id: int
    language: str


async def load_progress_destination(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    presentation_id: int,
) -> ProgressDestination | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    GenerationJob.id,
                    GenerationJob.progress_chat_id,
                    GenerationJob.progress_message_id,
                    Presentation.language,
                )
                .join(Presentation, Presentation.id == GenerationJob.presentation_id)
                .where(GenerationJob.presentation_id == presentation_id)
            )
        ).one_or_none()
    if row is None or row.progress_chat_id is None or row.progress_message_id is None:
        return None
    return ProgressDestination(
        generation_job_id=row.id,
        chat_id=row.progress_chat_id,
        message_id=row.progress_message_id,
        language=row.language,
    )


class ProgressBot(Protocol):
    async def edit_message_text(
        self,
        *,
        text: str,
        chat_id: int,
        message_id: int,
    ) -> object: ...


_PROGRESS_TEXTS: dict[str, dict[str, str]] = {
    "uz": {
        "content_and_design": "📝 Slaydlar tuzilmoqda...",
        "rendering_pptx": "📊 Grafik va jadvallar yaratilmoqda...",
        "converting_pdf": "📄 PDF tayyorlanmoqda...",
        "delivery_pending": "📤 Fayllar Telegram’ga yuborilmoqda...",
        "delivered": "✅ Tayyor!",
        "retrying": "♻️ Vaqtinchalik xato yuz berdi. Avtomatik qayta urinilmoqda...",
        "failed": "⚠️ Prezentatsiyani tayyorlashda xatolik yuz berdi.",
    },
    "ru": {
        "content_and_design": "📝 Формируются слайды...",
        "rendering_pptx": "📊 Создаются графики и таблицы...",
        "converting_pdf": "📄 Подготавливается PDF...",
        "delivery_pending": "📤 Файлы отправляются в Telegram...",
        "delivered": "✅ Готово!",
        "retrying": "♻️ Произошла временная ошибка. Выполняется повторная попытка...",
        "failed": "⚠️ Произошла ошибка при подготовке презентации.",
    },
    "en": {
        "content_and_design": "📝 Building the slides...",
        "rendering_pptx": "📊 Creating charts and tables...",
        "converting_pdf": "📄 Preparing the PDF...",
        "delivery_pending": "📤 Sending files to Telegram...",
        "delivered": "✅ Ready!",
        "retrying": "♻️ A temporary error occurred. Retrying automatically...",
        "failed": "⚠️ An error occurred while preparing the presentation.",
    },
}


class TelegramProgressReporter:
    def __init__(
        self,
        *,
        bot: ProgressBot,
        chat_id: int,
        message_id: int,
        language: str,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._language = language if language in _PROGRESS_TEXTS else "uz"

    async def __call__(self, progress: int, current_step: str) -> None:
        message = _PROGRESS_TEXTS[self._language].get(current_step, "⏳ Jarayon davom etmoqda...")
        text = message if current_step == "failed" else f"{progress}% · {message}"
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            )
        except TelegramAPIError:
            logger.warning(
                "telegram progress edit failed chat_id=%s message_id=%s progress=%s",
                self._chat_id,
                self._message_id,
                progress,
                exc_info=True,
            )
