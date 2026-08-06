"""Authenticated Telegram webhook with a durable update inbox."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apps.bot.models import TelegramUpdateInbox

router = APIRouter(prefix="/api/v1/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    expected = settings.telegram_webhook_secret.get_secret_value()
    supplied_values = request.headers.getlist("x-telegram-bot-api-secret-token")
    if (
        not expected
        or len(supplied_values) != 1
        or not hmac.compare_digest(supplied_values[0], expected)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    body = await request.body()
    if len(body) > 1_048_576:
        raise HTTPException(status_code=413, detail="update too large")
    try:
        update = Update.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Telegram update") from exc
    dispatcher: Dispatcher | None = getattr(request.app.state, "telegram_dispatcher", None)
    bot: Bot | None = getattr(request.app.state, "telegram_bot", None)
    if dispatcher is None or bot is None:
        raise HTTPException(status_code=503, detail="Telegram webhook is not initialized")
    if settings.bot_private_mode:
        event_context = UserContextMiddleware.resolve_event_context(update)
        if event_context.user is None or event_context.user.id != settings.owner_telegram_id:
            await dispatcher.feed_update(bot, update)
            return {"ok": True}

    body_hash = hashlib.sha256(body).hexdigest()
    lease_token = uuid4().hex
    now = datetime.now(UTC)
    try:
        async with request.app.state.session_factory() as session, session.begin():
            inbox = await session.scalar(
                select(TelegramUpdateInbox)
                .where(TelegramUpdateInbox.update_id == update.update_id)
                .with_for_update()
            )
            if inbox is None:
                inbox = TelegramUpdateInbox(
                    update_id=update.update_id,
                    body_sha256=body_hash,
                    status="PROCESSING",
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(minutes=5),
                    attempts=1,
                )
                session.add(inbox)
            else:
                if not hmac.compare_digest(inbox.body_sha256, body_hash):
                    raise HTTPException(status_code=409, detail="update ID payload conflict")
                if inbox.status == "COMPLETED":
                    return {"ok": True}
                expires_at = inbox.lease_expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if inbox.status == "PROCESSING" and expires_at is not None and expires_at > now:
                    raise HTTPException(status_code=503, detail="update is already processing")
                inbox.status = "PROCESSING"
                inbox.lease_token = lease_token
                inbox.lease_expires_at = now + timedelta(minutes=5)
                inbox.attempts += 1
                inbox.last_error = None
    except IntegrityError as exc:
        raise HTTPException(status_code=503, detail="concurrent update delivery") from exc

    try:
        await dispatcher.feed_update(bot, update)
    except Exception as exc:
        async with request.app.state.session_factory() as session, session.begin():
            inbox = await session.scalar(
                select(TelegramUpdateInbox)
                .where(TelegramUpdateInbox.update_id == update.update_id)
                .with_for_update()
            )
            if inbox is not None and inbox.lease_token == lease_token:
                inbox.status = "RECEIVED"
                inbox.lease_token = None
                inbox.lease_expires_at = None
                inbox.last_error = type(exc).__name__
        raise HTTPException(status_code=500, detail="update processing failed") from exc

    async with request.app.state.session_factory() as session, session.begin():
        inbox = await session.scalar(
            select(TelegramUpdateInbox)
            .where(TelegramUpdateInbox.update_id == update.update_id)
            .with_for_update()
        )
        if inbox is None or inbox.lease_token != lease_token:
            raise HTTPException(status_code=409, detail="update lease lost")
        inbox.status = "COMPLETED"
        inbox.completed_at = datetime.now(UTC)
        inbox.lease_token = None
        inbox.lease_expires_at = None
    return {"ok": True}
