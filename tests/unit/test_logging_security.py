import logging

from core.logging import configure_logging


def test_debug_mode_does_not_enable_third_party_http_request_bodies() -> None:
    logger_names = ("openai", "httpx", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        configure_logging(debug=True)
        for name in logger_names:
            assert logging.getLogger(name).level >= logging.WARNING
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)
