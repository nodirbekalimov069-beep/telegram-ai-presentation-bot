"""Deterministic HMAC-secured payment provider for development and tests."""

import hashlib
import hmac
import json
from collections.abc import Mapping

from modules.payments.providers.base import Invoice, PaymentWebhook


class InvalidMockWebhookSignature(ValueError):
    pass


class MockPaymentProvider:
    name = "mock"

    def __init__(self, *, secret: str, public_base_url: str = "https://mock.invalid/pay") -> None:
        if not secret:
            raise ValueError("mock payment secret must not be empty")
        self._secret = secret.encode()
        self._public_base_url = public_base_url.rstrip("/")

    async def create_invoice(
        self,
        *,
        order_public_id: str,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Invoice:
        return Invoice(
            provider=self.name,
            external_id=idempotency_key,
            payment_url=f"{self._public_base_url}/{order_public_id}",
            amount=amount,
            currency=currency,
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentWebhook:
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        received = normalized_headers.get("x-mock-signature", "")
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, expected):
            raise InvalidMockWebhookSignature("invalid mock webhook signature")
        payload = json.loads(body)
        return PaymentWebhook(
            idempotency_key=str(payload["event_id"]),
            provider_transaction_id=str(payload["transaction_id"]),
            order_public_id=str(payload["order_public_id"]),
            amount=int(payload["amount"]),
            currency=str(payload["currency"]),
            status=str(payload["status"]),
            raw_payload=payload,
        )

    async def transaction_status(self, provider_transaction_id: str) -> str:
        return "paid"

    async def cancel(self, provider_transaction_id: str) -> bool:
        return True

    async def refund(self, provider_transaction_id: str, amount: int) -> bool:
        return amount >= 0
