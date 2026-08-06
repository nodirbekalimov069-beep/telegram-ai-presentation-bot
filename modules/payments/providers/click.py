"""Click Shop API adapter with strict form parsing and mandated MD5 verification."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, parse_qsl

from modules.payments.providers.base import Invoice, PaymentWebhook


class InvalidClickRequest(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClickCommand:
    operation: str
    click_trans_id: str
    click_paydoc_id: str
    order_public_id: str
    amount_uzs: int
    merchant_prepare_id: int | None
    provider_error: int
    sign_time: str
    fields: dict[str, str]


class ClickProvider:
    name = "click"

    def __init__(self, *, merchant_id: str, service_id: str, secret_key: str) -> None:
        if not merchant_id or not service_id or not secret_key:
            raise ValueError("Click credentials must be configured")
        self._merchant_id = merchant_id
        self._service_id = service_id
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
            raise ValueError("Click supports positive integer UZS amounts")
        query = urlencode(
            {
                "service_id": self._service_id,
                "merchant_id": self._merchant_id,
                "amount": str(amount),
                "transaction_param": order_public_id,
            }
        )
        return Invoice(
            provider=self.name,
            external_id=idempotency_key,
            payment_url=f"https://my.click.uz/services/pay?{query}",
            amount=amount,
            currency=currency,
        )

    def authenticate_and_decode(self, body: bytes) -> ClickCommand:
        try:
            text = body.decode("utf-8", errors="strict")
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidClickRequest("malformed Click form") from exc
        fields: dict[str, str] = {}
        for key, value in pairs:
            if key in fields:
                raise InvalidClickRequest(f"duplicate Click field: {key}")
            fields[key] = value
        required = {
            "click_trans_id",
            "service_id",
            "click_paydoc_id",
            "merchant_trans_id",
            "amount",
            "action",
            "error",
            "sign_time",
            "sign_string",
        }
        if missing := required - fields.keys():
            raise InvalidClickRequest(f"missing Click fields: {sorted(missing)}")
        if fields["service_id"] != self._service_id:
            raise InvalidClickRequest("wrong Click service ID")
        action = fields["action"]
        if action not in {"0", "1"}:
            raise InvalidClickRequest("unsupported Click action")
        merchant_prepare_id: int | None = None
        if action == "1":
            raw_prepare_id = fields.get("merchant_prepare_id", "")
            try:
                merchant_prepare_id = int(raw_prepare_id)
            except ValueError as exc:
                raise InvalidClickRequest("invalid merchant_prepare_id") from exc
            if merchant_prepare_id <= 0:
                raise InvalidClickRequest("invalid merchant_prepare_id")
        amount_uzs = self._parse_amount(fields["amount"])
        preimage_parts = [
            fields["click_trans_id"],
            fields["service_id"],
            self._secret_key,
            fields["merchant_trans_id"],
        ]
        if action == "1":
            preimage_parts.append(str(merchant_prepare_id))
        preimage_parts.extend([fields["amount"], action, fields["sign_time"]])
        expected = hashlib.md5("".join(preimage_parts).encode()).hexdigest()  # noqa: S324
        if not hmac.compare_digest(fields["sign_string"].lower(), expected):
            raise InvalidClickRequest("invalid Click signature")
        try:
            provider_error = int(fields["error"])
        except ValueError as exc:
            raise InvalidClickRequest("invalid Click error code") from exc
        return ClickCommand(
            operation="PREPARE" if action == "0" else "COMPLETE",
            click_trans_id=fields["click_trans_id"],
            click_paydoc_id=fields["click_paydoc_id"],
            order_public_id=fields["merchant_trans_id"],
            amount_uzs=amount_uzs,
            merchant_prepare_id=merchant_prepare_id,
            provider_error=provider_error,
            sign_time=fields["sign_time"],
            fields=fields,
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentWebhook:
        del headers
        command = self.authenticate_and_decode(body)
        if command.operation != "COMPLETE" or command.provider_error != 0:
            raise InvalidClickRequest("Click request is not a successful Complete operation")
        return PaymentWebhook(
            idempotency_key=f"click:complete:{command.click_trans_id}",
            provider_transaction_id=command.click_trans_id,
            order_public_id=command.order_public_id,
            amount=command.amount_uzs,
            currency="UZS",
            status="paid",
            raw_payload=command.fields,
        )

    @staticmethod
    def _parse_amount(raw: str) -> int:
        try:
            value = Decimal(raw)
        except InvalidOperation as exc:
            raise InvalidClickRequest("invalid Click amount") from exc
        if not value.is_finite() or value <= 0 or value != value.to_integral_value():
            raise InvalidClickRequest("Click amount must be a positive integer UZS value")
        integer = int(value)
        if integer > 9_223_372_036_854_775_807:
            raise InvalidClickRequest("Click amount exceeds int64")
        return integer

    async def transaction_status(self, provider_transaction_id: str) -> str:
        raise NotImplementedError(
            "Click outbound status API requires a certified merchant endpoint"
        )

    async def cancel(self, provider_transaction_id: str) -> bool:
        raise NotImplementedError("Use Click reversal workflow")

    async def refund(self, provider_transaction_id: str, amount: int) -> bool:
        raise NotImplementedError("Use Click reversal workflow")
