"""Normalized logging for the GOaT model pipeline.

One shape for tail/grep/parse (and later Discord triage)::

    2026-09-06T13:59:39.123Z INFO synth-gen n=0 total=10000 rate=0.0

INFO and below go to stdout, WARNING and above to stderr (merged under
``nohup ... 2>&1``). ``--debug`` (or ``GOAT_DEBUG=1``) switches the level
to DEBUG. Stdlib-only. Logging must never crash its caller, leak secrets,
or forge lines — hence the guards below (each maps to a production failure).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone

_LOGGER_NAME: str = "goat_model"
_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_TIME_FMT: str = "%Y-%m-%dT%H:%M:%S.%f"
_DEBUG_FLAG: str = "--debug"
_DEBUG_ENV: str = "GOAT_DEBUG"
_DEBUG_ON: str = "1"
_SPLIT_LEVEL: int = logging.WARNING
_MAX_VALUE_LEN: int = 500
_SENSITIVE_KEYS: tuple[str, ...] = (
    "token",
    "secret",
    "passwd",
    "password",
    "api_key",
    "apikey",
    "webhook",
    "auth",
    "hf_token",
    "cookie",
    "session",
    "private_key",
)
_configured: bool = False
_lock = threading.Lock()


def _level_from_env() -> int:
    if _DEBUG_FLAG in sys.argv or os.environ.get(_DEBUG_ENV) == _DEBUG_ON:
        return logging.DEBUG
    return logging.INFO


class _DynamicStreamHandler(logging.StreamHandler):
    """StreamHandler that rebinds sys.stdout/stderr on every emit.

    Plain StreamHandler freezes whichever stream object exists at attach
    time, silently ignoring later redirection (pytest capsys, harness
    capture). Rebinding keeps tests and log capture honest.
    """

    def __init__(self, stream_name: str, level: int) -> None:
        super().__init__(stream=getattr(sys, stream_name))
        self._stream_name = stream_name
        self.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = getattr(sys, self._stream_name)
        super().emit(record)


def configure(level: int | None = None) -> logging.Logger:
    """Attach the stdout/stderr pair once; always refresh the level."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level if level is not None else _level_from_env())
    # NOTE: propagate stays False so Colab's root basicConfig never double
    # prints our lines; the cost (root-attached forwarders miss us) is
    # documented and accepted for batch runs.
    logger.propagate = False
    with _lock:
        if not _configured:
            plain = logging.Formatter("%(message)s")
            out = _DynamicStreamHandler("stdout", logging.DEBUG)
            out.addFilter(lambda record: record.levelno < _SPLIT_LEVEL)
            out.setFormatter(plain)
            err = _DynamicStreamHandler("stderr", _SPLIT_LEVEL)
            err.setFormatter(plain)
            logger.addHandler(out)
            logger.addHandler(err)
            _configured = True
    return logger


def _safe_str(value: object) -> str:
    try:
        text = str(value)
    except Exception:
        text = repr(value)
    if len(text) > _MAX_VALUE_LEN:
        text = text[:_MAX_VALUE_LEN] + f"...(+{len(text) - _MAX_VALUE_LEN})"
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEYS)


def format_line(level: int, tag: str, msg: object = "", kv: dict | None = None) -> str:
    """Render one normalized line; fail loud on bad shape, never on content."""
    if level not in _LEVELS.values():
        raise ValueError(f"unknown log level {level!r}")
    if not tag or not str(tag).strip():
        raise ValueError("log tag must be non-empty")
    if msg is None:
        msg = ""
    parts = [
        datetime.now(timezone.utc).strftime(_TIME_FMT)[:-3] + "Z",
        logging.getLevelName(level),
        _safe_str(tag),
        _safe_str(msg),
    ]
    for key, value in (kv or {}).items():
        name = _safe_str(key)
        parts.append(f"{name}=***" if _is_sensitive(name) else f"{name}={_safe_str(value)}")
    return " ".join(part for part in parts if part)


def log(level: int | str, tag: str, msg: object = "", **kv: object) -> None:
    """Emit one normalized event line; unknown levels fail loud."""
    if isinstance(level, str):
        key = level.strip().lower()
        if key not in _LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        level = _LEVELS[key]
    configure().log(level, format_line(level, tag, msg, dict(kv)))


def debug(tag: str, msg: object = "", **kv: object) -> None:
    log(logging.DEBUG, tag, msg, **kv)


def info(tag: str, msg: object = "", **kv: object) -> None:
    log(logging.INFO, tag, msg, **kv)


def warning(tag: str, msg: object = "", **kv: object) -> None:
    log(logging.WARNING, tag, msg, **kv)


def error(tag: str, msg: object = "", **kv: object) -> None:
    log(logging.ERROR, tag, msg, **kv)


def critical(tag: str, msg: object = "", **kv: object) -> None:
    log(logging.CRITICAL, tag, msg, **kv)
