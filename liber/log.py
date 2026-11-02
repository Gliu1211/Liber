"""Structured logging with credential redaction (CLAUDE.md hard rule 2, ARCHITECTURE.md §11, §15).

Why it exists: logs are evidence for diagnosing failures, so they should be structured
(key=value pairs, rendered as JSON) rather than free text. The first line of defence for
secrets is simply never passing headers to a logger; `redact_credentials` is a second line in
case someone does by mistake.

structlog is configured to hand its output to Python's standard `logging`, so Liber's events,
uvicorn's and httpx's all end up in the same place (and pytest's `caplog` sees all of them).
"""

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"

# Field names whose values are credentials. Compared case-insensitively.
SENSITIVE_KEYS = frozenset({"x-api-key", "authorization", "proxy-authorization", "cookie", "set-cookie", "api_key"})


def _is_sensitive(key: object) -> bool:
    return isinstance(key, str) and key.lower() in SENSITIVE_KEYS


def redact_credentials(logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor: mask credential values, including inside a nested `headers` dict."""
    for key, value in event_dict.items():
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        elif isinstance(value, Mapping):
            event_dict[key] = {k: REDACTED if _is_sensitive(k) else v for k, v in value.items()}
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Set up structlog once at startup. Safe to call more than once."""
    # basicConfig does nothing if logging is already configured (e.g. by pytest).
    logging.basicConfig(level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,  # turn log.exception(...) into a traceback
            redact_credentials,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
