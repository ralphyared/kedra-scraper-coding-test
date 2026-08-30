"""Structured logging (requirement 10): JSON lines to stdout and to a run file.

The important design choice here is that structlog is wired through the *stdlib*
logging module rather than using structlog's own printer.

Scrapy, boto3, pymongo and Dagster all log through stdlib `logging`. Routing
structlog through the same backend means their output is rendered as JSON too,
by the same formatter, into the same files -- instead of a readable JSON stream
from our code interleaved with unparseable plain-text lines from the libraries.
A run's logs are then a single machine-readable artifact, which is what makes
the found-vs-scraped reconciliation in requirement 1 mechanically checkable.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog

# Processors applied to events from both structlog and stdlib logging, so the
# two sources produce identically-shaped records.
_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    # Makes values bound with bind_contextvars (notably run_id) appear on every
    # event without threading a logger object through every call site.
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    # UTC and ISO-8601. Local timestamps in a log that may be read in another
    # timezone are a reliable way to lose an hour of an incident.
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def new_run_id() -> str:
    """Return a sortable, unique identifier for one pipeline run.

    Timestamp first so runs sort chronologically in a directory listing; a short
    random suffix so two runs starting in the same second cannot collide.
    """
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def configure_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    run_id: str | None = None,
) -> str:
    """Configure JSON logging for the process. Returns the run id in use.

    Emits to stdout (for the console and for Dagster to capture) and, when
    `log_dir` is given, to `<log_dir>/run-<run_id>.jsonl` as well.
    """
    run_id = run_id or new_run_id()

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            # Hands the event dict to the stdlib formatter below rather than
            # rendering it here. This is the bridge between the two systems.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Applied only to records that came from stdlib logging (Scrapy et al),
        # bringing them up to the same shape as structlog's own events.
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"run-{run_id}.jsonl", encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    # Replace rather than append: Scrapy installs its own handlers, and leaving
    # them attached duplicates every line in plain text alongside the JSON.
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level.upper())

    # Bound once here so every subsequent event in the process carries it, which
    # is what lets a single run be isolated from a shared log stream.
    structlog.contextvars.bind_contextvars(run_id=run_id)

    return run_id


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Thin wrapper, kept so call sites import from here."""
    return structlog.stdlib.get_logger(name)
