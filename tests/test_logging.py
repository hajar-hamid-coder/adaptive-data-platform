import json

import pytest
import structlog

from adp.errors import AdpError, ConfigError, IngestionError, QualityError, SchemaError
from adp.logging import bind_run_id, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _clean() -> None:
    structlog.contextvars.clear_contextvars()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, str]]:
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out]


def test_json_lines_have_run_id_level_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    rid = bind_run_id()
    log = get_logger("t")
    log.info("started", rows=3)
    log.warning("careful")
    lines = _lines(capsys)
    assert len(lines) == 2
    for rec in lines:
        assert {"run_id", "level", "event"} <= rec.keys()
        assert rec["run_id"] == rid
    assert lines[0]["level"] == "info" and lines[0]["event"] == "started"


def test_run_id_explicit_and_persists(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")
    assert bind_run_id("abc") == "abc"
    log = get_logger()
    log.info("one")
    log.debug("two")
    assert [r["run_id"] for r in _lines(capsys)] == ["abc", "abc"]


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ERROR")
    get_logger().info("hidden")
    assert capsys.readouterr().out == ""


def test_error_hierarchy() -> None:
    for exc in (ConfigError, SchemaError, QualityError, IngestionError):
        assert issubclass(exc, AdpError)
