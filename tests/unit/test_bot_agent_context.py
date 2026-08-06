from apps.bot.agent_context import payment_status_for_order
from modules.ai.presentation_agent import sanitize_conversation_data


def test_ai_cannot_write_server_authoritative_fields_into_conversation_state() -> None:
    cleaned = sanitize_conversation_data(
        {
            "topic": "AI",
            "slide_count": 8,
            "payment_status": "PAID",
            "free_demo_available": True,
            "admin_status": True,
            "order_id": "forged",
        }
    )

    assert cleaned == {"topic": "AI", "slide_count": 8}


def test_order_state_maps_to_backend_payment_gate() -> None:
    assert payment_status_for_order(status="WAITING_PAYMENT", amount=5000) == "WAITING_PAYMENT"
    assert payment_status_for_order(status="PAID", amount=5000) == "PAID"
    assert payment_status_for_order(status="OUTLINE_GENERATING", amount=5000) == "PAID"
    assert payment_status_for_order(status="CREATED", amount=0) == "NOT_REQUIRED"
