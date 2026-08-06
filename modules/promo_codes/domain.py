"""Promotion availability and integer-money discount rules."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DiscountType(StrEnum):
    PERCENT = "PERCENT"
    FIXED = "FIXED"


class InvalidPromoCode(ValueError):
    """Raised when a promo cannot be applied at the requested time."""


@dataclass(frozen=True, slots=True)
class PromoPolicy:
    code: str
    discount_type: DiscountType
    discount_value: int
    usage_limit: int | None
    used_count: int
    starts_at: datetime | None
    expires_at: datetime | None
    is_active: bool

    def __post_init__(self) -> None:
        if not isinstance(self.discount_value, int) or isinstance(self.discount_value, bool):
            raise TypeError("discount_value must be an integer")
        if self.discount_value < 0:
            raise ValueError("discount_value cannot be negative")
        if self.discount_type is DiscountType.PERCENT and self.discount_value > 100:
            raise ValueError("percentage discount cannot exceed 100")

    def ensure_available(self, *, at: datetime) -> None:
        if not self.is_active:
            raise InvalidPromoCode("promo code is inactive")
        if self.usage_limit is not None and self.used_count >= self.usage_limit:
            raise InvalidPromoCode("promo code usage limit reached")
        if self.starts_at is not None and at < self.starts_at:
            raise InvalidPromoCode("promo code is not active yet")
        if self.expires_at is not None and at >= self.expires_at:
            raise InvalidPromoCode("promo code has expired")

    def discounted_amount(self, amount_uzs: int, *, at: datetime) -> int:
        if not isinstance(amount_uzs, int) or isinstance(amount_uzs, bool):
            raise TypeError("amount_uzs must be an integer")
        if amount_uzs < 0:
            raise ValueError("amount_uzs cannot be negative")
        self.ensure_available(at=at)
        if self.discount_type is DiscountType.PERCENT:
            return amount_uzs * (100 - self.discount_value) // 100
        return max(0, amount_uzs - self.discount_value)
