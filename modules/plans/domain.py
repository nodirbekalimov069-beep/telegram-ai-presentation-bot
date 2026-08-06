"""Pure domain rules for tariff plans."""

from dataclasses import dataclass


class PlanLimitError(ValueError):
    """Raised when the requested slide count violates a plan limit."""


@dataclass(frozen=True, slots=True)
class PlanPolicy:
    code: str
    price_uzs: int
    minimum_slides: int
    maximum_slides: int
    priority: int

    def __post_init__(self) -> None:
        if isinstance(self.price_uzs, bool) or not isinstance(self.price_uzs, int):
            raise TypeError("price_uzs must be an integer")
        if self.price_uzs < 0:
            raise ValueError("price_uzs cannot be negative")
        if self.minimum_slides < 1 or self.maximum_slides < self.minimum_slides:
            raise ValueError("invalid slide limits")

    def validate_slide_count(self, slide_count: int) -> None:
        if not self.minimum_slides <= slide_count <= self.maximum_slides:
            raise PlanLimitError(
                f"plan {self.code!r} permits {self.minimum_slides}–"
                f"{self.maximum_slides} slides; received {slide_count}"
            )
