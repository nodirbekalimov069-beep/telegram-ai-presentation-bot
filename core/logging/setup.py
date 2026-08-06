"""Structured logging with secret redaction."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_PATTERN = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key)")


def redact_secrets(
    _: Any,
    __: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    for key in tuple(event_dict):
        if _SECRET_PATTERN.search(key):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(*, debug: bool = False) -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if debug else logging.INFO)
    for logger_name in ("openai", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
