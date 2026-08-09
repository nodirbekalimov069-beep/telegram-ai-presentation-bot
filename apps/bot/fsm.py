"""Persisted presentation creation FSM."""

from aiogram.fsm.state import State, StatesGroup


class PresentationFlow(StatesGroup):
    language = State()
    direction = State()
    topic = State()
    purpose = State()
    audience = State()
    knowledge_level = State()
    slide_count = State()
    design_style = State()
    additional_requirements = State()
    plan = State()
    waiting_payment = State()
    outline_review = State()
    outline_edit = State()


class ServiceFlow(StatesGroup):
    """One catalog-driven FSM shared by all non-presentation services."""

    collecting = State()
    confirming = State()
