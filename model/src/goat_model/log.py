"""Normalized logging for the GOaT model pipeline.

One line per event::

    2026-09-06 11:02:10 INFO synth-gen n=0 total=10000 rate=0.0

INFO and below go to stdout, WARNING and above to stderr (merged under
``nohup ... 2>&1``). ``--debug`` (or ``GOAT_DEBUG=1``) switches the level
to DEBUG. Stdlib-only, no third-party dependencies.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

_LOGGER_NAME = "goat"
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_configured = False


def _level_from_env() -> int:
    if "--debug" in sys.argv or os.environ.get("GOAT_DEBUG") == "1":
        return logging.DEBUG
    return logging.INFO


def configure(level: int | None = None) -> logging.Logger:
    """Attach the stdout/stderr handler pair once; idempotent."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level if level is not None else _level_from_env())
    logger.propagate = False
    if not _configured:
        plain = logging.Formatter("%(message)s")
        out = logging.StreamHandler(sys.stdout)
        out.setLevel(logging.DEBUG)
        out.addFilter(lambda record: record.levelno < logging.WARNING)
        out.setFormatter(plain)
        err = logging.StreamHandler(sys.stderr)
        err.setLevel(logging.WARNING)
        err.setFormatter(plain)
        logger.addHandler(out)
        logger.addHandler(err)
        _configured = True
    return logger


def format_line(level: int, tag: str, msg: str = "", kv: dict | None = None) -> str:
    """Render one normalized line (pure function, easy to test)."""
    parts = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        logging.getLevelName(level),
        tag,
        msg,
    ]
    parts.extend(f"{key}={value}" for key, value in (kv or {}).items())
    return " ".join(part for part in parts if part)


def log(level: int | str, tag: str, msg: str = "", **kv: object) -> None:
    """Emit one normalized event line."""
    if isinstance(level, str):
        level = _LEVELS[level.lower()]
    configure().log(level, format_line(level, tag, msg, dict(kv)))


def debug(tag: str, msg: str = "", **kv: object) -> None:
    log(logging.DEBUG, tag, msg, **kv)


def info(tag: str, msg: str = "", **kv: object) -> None:
    log(logging.INFO, tag, msg, **kv)


def warning(tag: str, msg: str = "", **kv: object) -> None:
    log(logging.WARNING, tag, msg, **kv)


warn = warning


def error(tag: str, msg: str = "", **kv: object) -> None:
    log(logging.ERROR, tag, msg, **kv)


def critical(tag: str, msg: str = "", **kv: object) -> None:
    log(logging.CRITICAL, tag, msg, **kv)
