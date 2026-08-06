"""Payment provider ports."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Invoice:
    provider: str
    external_id: str
    payment_url: str
    amount: int
    currency: str


@dataclass(frozen=True, slots=True)
class PaymentWebhook:
    idempotency_key: str
    provider_transaction_id: str
    order_public_id: str
    amount: int
    currency: str
    status: str
    raw_payload: dict[str, Any]


class PaymentProvider(Protocol):
    name: str

    async def create_invoice(
        self,
        *,
        order_public_id: str,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Invoice: ...

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentWebhook: ...

    async def transaction_status(self, provider_transaction_id: str) -> str: ...

    async def cancel(self, provider_transaction_id: str) -> bool: ...

    async def refund(self, provider_transaction_id: str, amount: int) -> bool: ...
