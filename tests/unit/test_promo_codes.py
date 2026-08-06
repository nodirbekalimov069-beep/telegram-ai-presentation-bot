from datetime import UTC, datetime, timedelta

import pytest

from modules.promo_codes.domain import DiscountType, InvalidPromoCode, PromoPolicy


def test_percentage_promo_uses_integer_arithmetic() -> None:
    now = datetime.now(UTC)
    promo = PromoPolicy(
        code="WELCOME10",
        discount_type=DiscountType.PERCENT,
        discount_value=10,
        usage_limit=100,
        used_count=2,
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
        is_active=True,
    )

    assert promo.discounted_amount(7_990, at=now) == 7_191


def test_fixed_discount_never_makes_amount_negative() -> None:
    now = datetime.now(UTC)
    promo = PromoPolicy(
        code="FREE",
        discount_type=DiscountType.FIXED,
        discount_value=10_000,
        usage_limit=None,
        used_count=0,
        starts_at=None,
        expires_at=None,
        is_active=True,
    )

    assert promo.discounted_amount(5_000, at=now) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"is_active": False},
        {"usage_limit": 1, "used_count": 1},
        {"starts_at": datetime.now(UTC) + timedelta(hours=1)},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
def test_unavailable_promo_is_rejected(changes: dict[str, object]) -> None:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "code": "PROMO",
        "discount_type": DiscountType.PERCENT,
        "discount_value": 10,
        "usage_limit": None,
        "used_count": 0,
        "starts_at": None,
        "expires_at": None,
        "is_active": True,
    }
    values.update(changes)
    promo = PromoPolicy(**values)  # type: ignore[arg-type]

    with pytest.raises(InvalidPromoCode):
        promo.discounted_amount(5_000, at=now)
