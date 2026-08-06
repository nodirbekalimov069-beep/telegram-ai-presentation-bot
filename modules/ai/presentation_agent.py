"""Trusted backend-aware contract for the Telegram presentation agent."""

import json
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class AgentMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


PaymentStatus = Literal[
    "NOT_REQUIRED",
    "WAITING_PAYMENT",
    "PAYMENT_PROCESSING",
    "PAID",
    "FAILED",
    "CANCELLED",
    "REFUNDED",
]


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=2, max_length=64)
    price_uzs: int = Field(ge=0)
    minimum_slides: int = Field(ge=1, le=30)
    maximum_slides: int = Field(ge=1, le=30)
    priority: int = Field(ge=0)


class AgentBackendContext(BaseModel):
    """Server-created values. Never construct this model from Telegram text."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    telegram_user_id: int
    selected_plan: AgentPlan | None = None
    payment_status: PaymentStatus | None = None
    free_demo_available: bool
    order_id: str | None = None
    presentation_id: str | None = None
    available_templates: list[str] = Field(default_factory=list)
    available_formats: list[Literal["pptx", "pdf"]] = Field(default_factory=list)
    admin_status: bool = False


class GenerationNotAllowed(ValueError):
    """Raised when server-authoritative generation gates are not satisfied."""


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reply_text: str = Field(min_length=1, max_length=4096)
    language: Literal["uz", "ru", "en"]
    stage: Literal[
        "collecting",
        "outline_review",
        "payment_required",
        "ready_to_generate",
        "completed",
        "error",
    ]
    collected_data: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    outline: list[dict[str, Any]] | None = None
    outline_approved: bool = False


def load_system_prompt() -> str:
    path = Path(__file__).with_name("prompts") / "presentation_agent_uz.md"
    return path.read_text(encoding="utf-8")


def build_agent_messages(
    *,
    user_text: str,
    backend: AgentBackendContext,
    conversation_data: dict[str, Any],
) -> list[AgentMessage]:
    trusted_context = backend.model_dump_json(exclude_none=False)
    untrusted_payload = json.dumps(
        {"user_text": user_text, "conversation_data": conversation_data},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [
        {"role": "system", "content": load_system_prompt()},
        {"role": "system", "content": trusted_context},
        {"role": "user", "content": untrusted_payload},
    ]


_SERVER_AUTHORITATIVE_FIELDS = frozenset(
    {
        "selected_plan",
        "plan_limits",
        "payment_status",
        "free_demo_available",
        "order_id",
        "presentation_id",
        "admin_status",
        "telegram_user_id",
        "available_templates",
        "available_formats",
    }
)


def sanitize_conversation_data(data: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that must only come from backend/database state."""
    return {key: value for key, value in data.items() if key not in _SERVER_AUTHORITATIVE_FIELDS}


def assert_generation_allowed(
    backend: AgentBackendContext,
    *,
    slide_count: int,
    outline_slide_count: int,
) -> None:
    if backend.payment_status not in {"PAID", "NOT_REQUIRED"}:
        raise GenerationNotAllowed("backend payment status does not permit generation")
    if not backend.order_id:
        raise GenerationNotAllowed("backend order_id is required")
    if not backend.presentation_id:
        raise GenerationNotAllowed("backend presentation_id is required")
    if backend.selected_plan is None:
        raise GenerationNotAllowed("an active backend plan is required")
    plan = backend.selected_plan
    if not plan.minimum_slides <= slide_count <= plan.maximum_slides:
        raise GenerationNotAllowed("slide count violates the backend plan limit")
    if outline_slide_count != slide_count:
        raise GenerationNotAllowed("outline count must equal slide_count")
