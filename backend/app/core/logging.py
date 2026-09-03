"""structlog configuration: JSON in production, human-readable in development.

Both structlog calls and plain stdlib records (uvicorn, SQLAlchemy, anything
a library emits) go through the same processor chain and the same renderer,
so a production log stream is uniformly parseable.
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.config import settings

#: Libraries whose DEBUG output would leak document contents or drown the log.
#: Pinned explicitly so they never inherit a lowered root level.
#:
#: pdfminer is the one that matters. It logs every token it reads, so setting
#: LOG_LEVEL=DEBUG to investigate an incident would write candidates' names,
#: emails and phone numbers into the log stream verbatim, one line at a time —
#: measured at 678 KB of it for a single one-page resume.
THIRD_PARTY_LOG_LEVELS: dict[str, int] = {
    "pdfminer": logging.WARNING,
    "pdfplumber": logging.WARNING,
    "PIL": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "asyncio": logging.INFO,
}

#: Correlation id for the request currently being handled, set by the middleware.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def bind_request_id(request_id: str) -> None:
    """Attach ``request_id`` to every log record emitted for this request."""
    request_id_var.set(request_id)


def get_request_id() -> str | None:
    """Return the correlation id of the current request, if any."""
    return request_id_var.get()


def _add_request_id(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """structlog processor injecting the current request id, when there is one."""
    request_id = request_id_var.get()
    if request_id is not None:
        event_dict["request_id"] = request_id
    return event_dict


def _shared_processors() -> list[Processor]:
    """Processors applied to structlog and stdlib records alike."""
    return [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging() -> None:
    """Configure structlog and route the stdlib logging tree through it."""
    shared = _shared_processors()

    renderer: Processor
    final: list[Processor]
    if settings.is_production:
        # ensure_ascii=False on purpose. The default escapes every Cyrillic
        # character into a numeric sequence, which is valid JSON but invisible
        # to any grep-based redaction rule or PII scan written against literal
        # text — and in this service the personal data is mostly Cyrillic.
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
        final = [structlog.processors.format_exc_info, renderer]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        final = [renderer]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                *final,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # uvicorn ships its own handlers; drop them so nothing is logged twice.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        noisy_logger = logging.getLogger(noisy)
        noisy_logger.handlers.clear()
        noisy_logger.propagate = True

    # Libraries verbose enough to be a privacy problem rather than just noise.
    for chatty, level in THIRD_PARTY_LOG_LEVELS.items():
        logging.getLogger(chatty).setLevel(level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger; prefer ``get_logger(__name__)`` in modules."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
