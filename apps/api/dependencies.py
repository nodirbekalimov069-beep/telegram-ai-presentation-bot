"""FastAPI dependencies."""

from collections.abc import AsyncIterator
import hmac
from typing import cast

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_settings_from_request(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def require_admin_header(
    request: Request,
    x_telegram_user_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> int:
    try:
        telegram_user_id = int(x_telegram_user_id or "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required") from exc
    settings: Settings = request.app.state.settings
    if telegram_user_id not in settings.admin_id_set:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    configured_token = settings.admin_api_token.get_secret_value()
    if not configured_token or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    supplied = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, configured_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return telegram_user_id
