"""Unit tests: normalized logger line shape, stream split, debug detection."""
import logging
import sys
from pathlib import Path

# Ensure model/src is on sys.path BEFORE any goat_model import
_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1] / "src"  # model/src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import goat_model.log as gl


def test_format_line_shape():
    line = gl.format_line(logging.INFO, "synth-gen", "progress", {"n": 0, "total": 10000})
    assert line.endswith("INFO synth-gen progress n=0 total=10000")
    head = line[:19]
    assert len(head) == 19 and head[4] == "-" and head[13] == ":"  # YYYY-MM-DD HH:MM:SS


def test_format_line_skips_empty_msg():
    line = gl.format_line(logging.ERROR, "train_mt", "", {"reason": "x"})
    assert "ERROR train_mt reason=x" in line


def test_level_name_mapping():
    assert gl._LEVELS["warn"] == logging.WARNING
    assert gl._LEVELS["debug"] == logging.DEBUG


def test_configure_idempotent():
    first = gl.configure(level=logging.INFO)
    second = gl.configure(level=logging.INFO)
    assert first is second
    assert len(first.handlers) == 2


def test_debug_detection_argv(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["prog", "--debug"])
    with caplog.at_level(logging.DEBUG, logger="goat"):
        gl.configure()
        gl.debug("probe", "detail")
    assert any("probe detail" in r.message for r in caplog.records)


def test_default_level_info_without_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.delenv("GOAT_DEBUG", raising=False)
    assert gl._level_from_env() == logging.INFO


def test_stdout_stderr_split():
    logger = gl.configure(level=logging.DEBUG)
    out, err = logger.handlers[0], logger.handlers[1]
    assert out.level == logging.DEBUG and err.level == logging.WARNING
    assert getattr(out, "stream", None) is sys.stdout
    assert getattr(err, "stream", None) is sys.stderr


if __name__ == "__main__":
    test_format_line_shape()
    test_format_line_skips_empty_msg()
    test_level_name_mapping()
    test_configure_idempotent()
    test_stdout_stderr_split()
    print("log smoke ok")
