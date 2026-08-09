"""Import every ORM model so Alembic and tests see complete metadata."""

from modules.admin.models import AuditLog, BotSetting, PromptVersion
from modules.orders.models import Order
from modules.payments.models import Payment, PaymentEvent
from modules.plans.models import Plan
from modules.presentations.models import GenerationAttempt, GenerationJob, Presentation
from modules.presentations.ratings import Rating
from modules.promo_codes.models import PromoCode
from modules.service_orders.models import ServiceOrder
from modules.templates.models import Template
from modules.users.models import FreeDemoClaim, User
from core.outbox.models import OutboxEvent
from apps.bot.models import TelegramUpdateInbox

__all__ = [
    "AuditLog",
    "BotSetting",
    "FreeDemoClaim",
    "GenerationAttempt",
    "GenerationJob",
    "Order",
    "OutboxEvent",
    "Payment",
    "PaymentEvent",
    "Plan",
    "Presentation",
    "PromoCode",
    "PromptVersion",
    "Rating",
    "ServiceOrder",
    "TelegramUpdateInbox",
    "Template",
    "User",
]
