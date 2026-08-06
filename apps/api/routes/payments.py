"""Payment webhook endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from modules.payments.protocol_service import PaymentProtocolService, PaymeProtocolError
from modules.payments.providers.click import ClickProvider, InvalidClickRequest
from modules.payments.providers.mock import MockPaymentProvider
from modules.payments.providers.payme import InvalidPaymeRequest, PaymeProvider
from modules.payments.service import (
    PaymentAmountMismatch,
    PaymentOrderNotFound,
    PaymentService,
    WebhookSignatureError,
)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


def _payme_error(
    rpc_id: object,
    code: int,
    message: str,
    data: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": {"uz": message, "ru": message, "en": message},
    }
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


@router.post("/payme")
async def payme_webhook(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    body = await request.body()
    if len(body) > 1_048_576:
        return _payme_error(None, -32700, "Parse error")
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1:
        return _payme_error(None, -32504, "Insufficient privileges")
    try:
        provider = PaymeProvider(
            merchant_id=settings.payme_merchant_id.get_secret_value(),
            login=settings.payme_login,
            secret_key=settings.payme_secret_key.get_secret_value(),
        )
        command = provider.authenticate_and_decode(
            body,
            {"authorization": authorization_values[0]},
        )
    except (ValueError, InvalidPaymeRequest) as exc:
        code = -32504 if "auth" in str(exc).lower() or "credential" in str(exc).lower() else -32700
        return _payme_error(
            None, code, "Insufficient privileges" if code == -32504 else "Parse error"
        )
    async with request.app.state.session_factory() as session:
        try:
            result = await PaymentProtocolService().handle_payme(session, command)
        except PaymeProtocolError as exc:
            return _payme_error(command.rpc_id, exc.code, exc.message, exc.data)
    if command.method == "GetStatement":
        result = {"transactions": result}
    return {"jsonrpc": "2.0", "id": command.rpc_id, "result": result}


@router.post("/click")
async def click_webhook(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    body = await request.body()
    if len(body) > 65_536:
        return {
            "click_trans_id": "0",
            "merchant_trans_id": "0",
            "merchant_prepare_id": 0,
            "error": -1,
            "error_note": "Invalid request",
        }
    try:
        provider = ClickProvider(
            merchant_id=settings.click_merchant_id.get_secret_value(),
            service_id=settings.click_service_id.get_secret_value(),
            secret_key=settings.click_secret_key.get_secret_value(),
        )
        command = provider.authenticate_and_decode(body)
    except (ValueError, InvalidClickRequest):
        return {
            "click_trans_id": "0",
            "merchant_trans_id": "0",
            "merchant_prepare_id": 0,
            "error": -1,
            "error_note": "SIGN CHECK FAILED!",
        }
    async with request.app.state.session_factory() as session:
        return await PaymentProtocolService().handle_click(session, command)


@router.post("/mock/webhook")
async def mock_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    provider = MockPaymentProvider(secret=settings.mock_payment_secret.get_secret_value())
    body = await request.body()
    async with request.app.state.session_factory() as session:
        try:
            result = await PaymentService().handle_webhook(
                session,
                provider,
                body,
                dict(request.headers),
            )
        except WebhookSignatureError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook"
            ) from exc
        except (PaymentAmountMismatch, PaymentOrderNotFound) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"applied": result.applied, "duplicate": result.duplicate}
