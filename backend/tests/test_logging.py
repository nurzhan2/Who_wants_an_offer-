"""Logging is structured, level-aware and carries the request id."""

import json
from collections.abc import Iterator

import pytest
import structlog

from app.core import logging as logging_module
from app.core.logging import (
    bind_request_id,
    configure_logging,
    get_logger,
    get_request_id,
    request_id_var,
)


@pytest.fixture(autouse=True)
def _isolated_structlog() -> Iterator[None]:
    """Never let one test's logging configuration leak into the next."""
    token = request_id_var.set(None)
    try:
        yield
    finally:
        request_id_var.reset(token)
        structlog.reset_defaults()


def test_request_id_round_trip() -> None:
    """The correlation id is readable anywhere in the same context."""
    assert get_request_id() is None

    bind_request_id("req-1")

    assert get_request_id() == "req-1"


def test_production_logs_are_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production output must be machine-parseable, one JSON object per line."""
    monkeypatch.setattr(logging_module.settings, "environment", "production")
    monkeypatch.setattr(logging_module.settings, "log_level", "INFO")
    configure_logging()
    bind_request_id("req-json")

    get_logger("test").info("pipeline_finished", source_slug="hh", found=42)

    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "pipeline_finished"
    assert record["source_slug"] == "hh"
    assert record["found"] == 42
    assert record["request_id"] == "req-json"
    assert record["level"] == "info"
    assert record["timestamp"]


def test_development_logs_are_human_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Development output is for eyes, not for parsers."""
    monkeypatch.setattr(logging_module.settings, "environment", "development")
    monkeypatch.setattr(logging_module.settings, "log_level", "INFO")
    configure_logging()

    get_logger("test").info("hello", answer=42)

    captured = capsys.readouterr().err
    assert "hello" in captured
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.strip().splitlines()[-1])


def test_log_level_is_respected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A debug line must not appear when the configured level is WARNING."""
    monkeypatch.setattr(logging_module.settings, "environment", "production")
    monkeypatch.setattr(logging_module.settings, "log_level", "WARNING")
    configure_logging()

    logger = get_logger("test")
    logger.debug("invisible")
    logger.warning("visible")

    captured = capsys.readouterr().err
    assert "invisible" not in captured
    assert "visible" in captured


def test_request_id_absent_when_unbound(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Background jobs have no request; the field is omitted rather than null."""
    monkeypatch.setattr(logging_module.settings, "environment", "production")
    monkeypatch.setattr(logging_module.settings, "log_level", "INFO")
    configure_logging()

    get_logger("test").info("scheduler_tick")

    record = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "request_id" not in record
