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
    color_style = State()
    image_amount = State()
    diagram = State()
    file_format = State()
    plan = State()
    waiting_payment = State()
    outline_review = State()
    outline_edit = State()
    queued = State()
    rating = State()
