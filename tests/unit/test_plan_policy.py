import pytest

from modules.plans.domain import PlanLimitError, PlanPolicy


@pytest.mark.parametrize(
    ("code", "price_uzs", "minimum", "maximum", "valid_count"),
    [
        ("free_demo", 0, 1, 8, 8),
        ("medium", 3_000, 10, 15, 12),
        ("premium", 5_000, 15, 20, 18),
        ("premium_plus", 7_990, 20, 30, 25),
    ],
)
def test_plan_policy_accepts_slide_count_within_database_limits(
    code: str,
    price_uzs: int,
    minimum: int,
    maximum: int,
    valid_count: int,
) -> None:
    plan = PlanPolicy(
        code=code,
        price_uzs=price_uzs,
        minimum_slides=minimum,
        maximum_slides=maximum,
        priority=1,
    )

    plan.validate_slide_count(valid_count)


@pytest.mark.parametrize("slide_count", [9, 0, -1])
def test_free_demo_rejects_slide_count_outside_limits(slide_count: int) -> None:
    plan = PlanPolicy(
        code="free_demo",
        price_uzs=0,
        minimum_slides=1,
        maximum_slides=8,
        priority=1,
    )

    with pytest.raises(PlanLimitError):
        plan.validate_slide_count(slide_count)


def test_plan_policy_rejects_float_money() -> None:
    with pytest.raises(TypeError, match="integer"):
        PlanPolicy(
            code="medium",
            price_uzs=3_000.0,  # type: ignore[arg-type]
            minimum_slides=10,
            maximum_slides=15,
            priority=10,
        )
