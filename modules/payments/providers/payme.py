"""Payme Merchant API adapter with strict Basic auth and JSON-RPC decoding."""

import base64
import binascii
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from modules.payments.providers.base import Invoice, PaymentWebhook


class InvalidPaymeRequest(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaymeCommand:
    method: str
    rpc_id: str | int | None
    transaction_id: str | None
    order_public_id: str | None
    amount_tiyin: int | None
    provider_time_ms: int | None
    reason: int | None
    params: dict[str, Any]


class PaymeProvider:
    name = "payme"
    _methods = {
        "CheckPerformTransaction",
        "CreateTransaction",
        "PerformTransaction",
        "CancelTransaction",
        "CheckTransaction",
        "GetStatement",
    }

    def __init__(self, *, merchant_id: str, login: str, secret_key: str) -> None:
        if not merchant_id or not login or not secret_key:
            raise ValueError("Payme credentials must be configured")
        self._merchant_id = merchant_id
        self._login = login
        self._secret_key = secret_key

    async def create_invoice(
        self,
        *,
        order_public_id: str,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Invoice:
        if currency != "UZS" or isinstance(amount, bool) or amount <= 0:
            raise ValueError("Payme supports positive integer UZS amounts")
        payload = f"m={self._merchant_id};ac.order_id={order_public_id};a={amount * 100}"
        encoded = base64.b64encode(payload.encode()).decode()
        return Invoice(
            provider=self.name,
            external_id=idempotency_key,
            payment_url=f"https://checkout.paycom.uz/{encoded}",
            amount=amount,
            currency=currency,
        )

    def authenticate_and_decode(
        self,
        body: bytes,
        headers: Mapping[str, str],
    ) -> PaymeCommand:
        normalized = {key.lower(): value for key, value in headers.items()}
        authorization = normalized.get("authorization", "")
        if not authorization.startswith("Basic "):
            raise InvalidPaymeRequest("Payme authorization is missing")
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise InvalidPaymeRequest("malformed Payme authorization") from exc
        login, separator, password = decoded.partition(":")
        if (
            not separator
            or not hmac.compare_digest(login, self._login)
            or not hmac.compare_digest(password, self._secret_key)
        ):
            raise InvalidPaymeRequest("invalid Payme credentials")
        try:
            payload = json.loads(body, object_pairs_hook=self._reject_duplicate_keys)
        except (json.JSONDecodeError, UnicodeDecodeError, InvalidPaymeRequest) as exc:
            raise InvalidPaymeRequest("malformed Payme JSON-RPC request") from exc
        if not isinstance(payload, dict):
            raise InvalidPaymeRequest("Payme JSON-RPC body must be an object")
        method = payload.get("method")
        if method not in self._methods:
            raise InvalidPaymeRequest("unsupported Payme method")
        params = payload.get("params")
        if not isinstance(params, dict):
            raise InvalidPaymeRequest("Payme params must be an object")
        amount = params.get("amount")
        if amount is not None and (type(amount) is not int or amount <= 0):
            raise InvalidPaymeRequest("Payme amount must be a positive integer tiyin value")
        account = params.get("account", {})
        if not isinstance(account, dict):
            raise InvalidPaymeRequest("Payme account must be an object")
        order_ref = account.get("order_id")
        if order_ref is not None and (not isinstance(order_ref, str) or not order_ref):
            raise InvalidPaymeRequest("invalid Payme order account")
        transaction_id = params.get("id")
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id
        ):
            raise InvalidPaymeRequest("invalid Payme transaction ID")
        provider_time = params.get("time")
        if provider_time is not None and (type(provider_time) is not int or provider_time < 0):
            raise InvalidPaymeRequest("invalid Payme transaction time")
        reason = params.get("reason")
        if reason is not None and type(reason) is not int:
            raise InvalidPaymeRequest("invalid Payme cancellation reason")
        return PaymeCommand(
            method=method,
            rpc_id=payload.get("id"),
            transaction_id=transaction_id,
            order_public_id=order_ref,
            amount_tiyin=amount,
            provider_time_ms=provider_time,
            reason=reason,
            params=params,
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentWebhook:
        command = self.authenticate_and_decode(body, headers)
        raise InvalidPaymeRequest(
            f"Payme {command.method} requires the stateful Merchant API service"
        )

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidPaymeRequest(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    async def transaction_status(self, provider_transaction_id: str) -> str:
        raise NotImplementedError("Payme queries this application through CheckTransaction")

    async def cancel(self, provider_transaction_id: str) -> bool:
        raise NotImplementedError("Payme cancellation is an inbound Merchant API operation")

    async def refund(self, provider_transaction_id: str, amount: int) -> bool:
        raise NotImplementedError("Initiate Payme refunds in the merchant cabinet")
