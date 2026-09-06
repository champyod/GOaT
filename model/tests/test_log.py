"""Unit tests: normalized logger behavior (never internals)."""
import logging
import sys
from pathlib import Path

import pytest

# Ensure model/src is on sys.path BEFORE any goat_model import
_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1] / "src"  # model/src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import goat_model.log as log_module
from goat_model.log import configure, error, format_line, info, log


@pytest.fixture(autouse=True)
def _isolated_logger():
    """Snapshot and restore global logger state so tests never leak."""
    logger = logging.getLogger("goat_model")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_configured = log_module._configured
    logger.handlers.clear()
    log_module._configured = False
    try:
        yield
    finally:
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.setLevel(saved_level)
        log_module._configured = saved_configured


def test_line_shape() -> None:
    line = format_line(logging.INFO, "synth-gen", "progress", {"n": 0, "total": 10000})
    assert line[:4].isdigit() and "Z INFO synth-gen progress n=0 total=10000" in line


def test_unknown_level_fails_loud() -> None:
    with pytest.raises(ValueError):
        log("bogus", "tag")
    with pytest.raises(ValueError):
        format_line(9999, "tag")


def test_empty_tag_fails_loud() -> None:
    with pytest.raises(ValueError):
        format_line(logging.INFO, "")
    with pytest.raises(ValueError):
        format_line(logging.INFO, None)  # type: ignore[arg-type]


def test_newlines_cannot_forge_lines() -> None:
    line = format_line(logging.INFO, "tag", "a\n2026-01-01T00:00:00 FAKE x", {"k": "v\nw"})
    assert line.count("\n") == 0


def test_secrets_redacted() -> None:
    line = format_line(logging.INFO, "tag", "", {"api_key": "sk-live-123", "path": "/x"})
    assert "sk-live-123" not in line
    assert "api_key=***" in line
    assert "path=/x" in line


def test_long_values_truncated() -> None:
    line = format_line(logging.INFO, "tag", "x" * 600)
    assert len(line) < 700
    assert "...(+" in line


def test_streams_split(capsys: pytest.CaptureFixture) -> None:
    info("tag", "out-line")
    error("tag", "err-line")
    captured = capsys.readouterr()
    assert "out-line" in captured.out
    assert "err-line" not in captured.out
    assert "err-line" in captured.err


def test_debug_gated_by_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.delenv("GOAT_DEBUG", raising=False)
    configure()
    log_module.debug("tag", "hidden")
    assert "hidden" not in capsys.readouterr().out


def test_double_configure_updates_level() -> None:
    configure(level=logging.INFO)
    configure(level=logging.DEBUG)
    logger = logging.getLogger("goat_model")
    assert logger.level == logging.DEBUG
    ours = [h for h in logger.handlers if type(h).__name__ == "_DynamicStreamHandler"]
    assert len(ours) == 2


def test_wrapper_verbs(capsys: pytest.CaptureFixture) -> None:
    from goat_model.utils import log_call

    @log_call
    def _probe() -> str:
        return "ok"

    assert _probe() == "ok"
    out = capsys.readouterr().out
    assert "enter" in out and "exit" in out
