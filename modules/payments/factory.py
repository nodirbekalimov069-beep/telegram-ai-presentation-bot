"""Payment provider dependency factory."""

from core.config import Settings
from modules.payments.providers.base import PaymentProvider
from modules.payments.providers.click import ClickProvider
from modules.payments.providers.mock import MockPaymentProvider
from modules.payments.providers.payme import PaymeProvider


def build_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.payment_provider == "mock":
        return MockPaymentProvider(
            secret=settings.mock_payment_secret.get_secret_value(),
            public_base_url=f"{settings.public_base_url.rstrip('/')}/api/v1/payments/mock/pay",
        )
    if settings.payment_provider == "click":
        return ClickProvider(
            merchant_id=settings.click_merchant_id.get_secret_value(),
            service_id=settings.click_service_id.get_secret_value(),
            secret_key=settings.click_secret_key.get_secret_value(),
        )
    return PaymeProvider(
        merchant_id=settings.payme_merchant_id.get_secret_value(),
        login=settings.payme_login,
        secret_key=settings.payme_secret_key.get_secret_value(),
    )
