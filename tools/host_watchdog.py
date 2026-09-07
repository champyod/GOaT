#!/usr/bin/env python3
"""Host-side watchdog for long batch jobs.

Tails a log FILE (local path or synced copy) and reports to a webhook:
error lines matched, a done line, silence past ``--silence`` (may-be-down
warning), or silence past ``--downtime`` (really-down error). Runs on the
operator machine, independent of the job host, so it still reports when
the job host itself goes away.

Never exits on its own: warning/downtime alerts latch once per silent
episode and re-arm on new growth; silence after a reported error or a
seen done line is expected and stays quiet (stdout heartbeat shows
``stuck at error`` / ``done, watching`` instead). Ctrl+C to stop.

Usage:
    export DISCORD_WEBHOOK_URL=...  # or model/.env, or --webhook
    python tools/host_watchdog.py --log ~/synced/goat.log \\
        --silence 900 --downtime 3600 --poll 15
    # per-poll stdout: INFO watchdog <state> idle=.. size=.. offset=.. [last=..]
    # --error-pattern / --done-pattern are repeatable, substring, case-insensitive.

Sends fail-open: a dead webhook never stops the watch. Webhook URL comes
from ``DISCORD_WEBHOOK_URL`` env or ``--webhook`` (explicit flag wins —
either way it is never logged).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

try:  # pipeline logging when run inside the model tree
    from goat_model.log import error as _err
    from goat_model.log import info as _info
    from goat_model.log import warning as _warn
except ImportError:  # standalone copy (e.g. Pi): same call shape, plain stdout

    def _info(tag: str, msg: object = "", **kv: object) -> None:
        tail = " ".join(f"{k}={v}" for k, v in kv.items())
        print(f"INFO {tag} {msg} {tail}".rstrip(), flush=True)

    def _warn(tag: str, msg: object = "", **kv: object) -> None:
        tail = " ".join(f"{k}={v}" for k, v in kv.items())
        print(f"WARNING {tag} {msg} {tail}".rstrip(), flush=True)

    def _err(tag: str, msg: object = "", **kv: object) -> None:
        tail = " ".join(f"{k}={v}" for k, v in kv.items())
        print(f"ERROR {tag} {msg} {tail}".rstrip(), flush=True)


def _send(webhook: str, text: str, title: str = "GOaT", color: int = 0x5865F2, ping: bool = False) -> None:
    if not webhook:
        return
    import datetime
    role = "1498710581547634930"
    try:
        payload = {
            "content": f"<@&{role}>" if ping else "",
            "allowed_mentions": {"parse": ["roles"]},
            "embeds": [{
                "title": title,
                "description": text[:4000],
                "color": color,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }],
        }
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "goat-watchdog/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as err:
        _warn("watchdog", f"notify failed: {err}")





def _load_dotenv() -> None:
    """Load model/.env (KEY=VALUE) into environ; real exports win. Stdlib-only."""
    import os as _os
    from pathlib import Path as _Path
    cands = [_Path.cwd() / ".env", _Path(__file__).resolve().parent.parent / ".env"]
    for _p in _os.environ.get("GOAT_ENV", "").split(":") if _os.environ.get("GOAT_ENV") else []:
        cands.append(_Path(_p))
    for cand in cands:
        try:
            text = cand.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\'").strip('"')
            if key and key not in _os.environ:
                _os.environ[key] = val


def _matches_any(line: str, patterns: list[str]) -> str | None:
    lowered = line.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            return pattern
    return None


def _fmt_age(seconds: float) -> str:
    """Human age like 45s, 2m 3s, 1h 5m for idle/sync displays."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _tail_new(path: Path, offset: int) -> tuple[str | None, int]:
    """New bytes since offset; (None, size) when unchanged/unreadable. Resets on rotation."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, offset
    if size < offset:
        offset = 0  # rotated/truncated: reread from start
    if size == offset:
        return None, offset
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read(), size
    except OSError:
        return None, offset


def _sync_age(sync_file: Path | None) -> tuple[float | None, str]:
    """(age_seconds, clock_time) of the sync heartbeat; (None, '-') when off/missing."""
    if sync_file is None:
        return None, "-"
    try:
        at = sync_file.stat().st_mtime
    except OSError:
        return None, "-"
    return time.time() - at, time.strftime("%H:%M:%S", time.localtime(at))


def _heartbeat_state(*, finished: bool, last_event: str | None) -> str:
    if finished:
        return "done, watching"
    if last_event == "error":
        return "stuck at error, waiting for recovery"
    return "healthy"


def _ingest_chunk(chunk: str, *, job: str, webhook: str, error_patterns: list[str],
                  done_patterns: list[str], reported: set[str]) -> tuple[str | None, str | None, bool]:
    """Scan fresh lines (logs + Discord as side effects). Returns (last_progress, event, finished)."""
    progress: str | None = None
    event: str | None = None
    finished = False
    for line in chunk.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        progress = re.sub(r"^\S+ (?:INFO|WARNING|ERROR) ", "", stripped).strip() or stripped
        hit = _matches_any(line, error_patterns)
        if hit is not None and line not in reported:
            reported.add(line)
            event = "error"
            _err("watchdog", line.strip()[:200], match=hit)
            _send(webhook, f"{job} ERROR ({hit}): {line.strip()[:1000]}", title="Training ERROR", color=0xFF0000, ping=True)
            continue
        if _matches_any(line, done_patterns) is not None:
            finished = True
            _info("watchdog", line.strip()[:200], state="done")
            _send(webhook, f"{job} done", title="Training done", color=0x00FF00, ping=True)
    return progress, event, finished


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Watch a batch log file and report to a webhook.")
    parser.add_argument("--log", type=Path, default=Path("~/synced/goat.log"),
                        help="log file to tail (default: ~/synced/goat.log)")
    parser.add_argument("--webhook", default="", help="webhook URL (or DISCORD_WEBHOOK_URL env)")
    parser.add_argument("--job", default="goat", help="job label used in Discord messages")
    # NOTE: bare "oom" (matches "room"), "inf" (matches "info") and "nan"
    # (matches "finance") deliberately excluded; covered by "out of memory",
    # "=inf" and "=nan" instead.
    parser.add_argument("--error-pattern", action="append",
                        default=["traceback", "error", "exception", "fail", "fatal",
                                 "abort", "killed", "out of memory", "cuda out of memory",
                                 "illegal memory access", "segmentation fault", "segfault",
                                 "core dumped", "timeout", "timed out", "connection refused",
                                 "connection reset", "permission denied", "no space left",
                                 "read-only file system", "panic", "uncaught", "crash",
                                 "=nan", "=inf"],
                        help="repeatable substring (case-insensitive) signalling failure")
    parser.add_argument("--done-pattern", action="append",
                        default=["done", "finish", "complet", "success"],
                        help="repeatable substring signalling clean finish")
    parser.add_argument("--silence", type=float, default=600.0,
                        help="seconds without growth = may-be-down warning (latched, never exits)")
    parser.add_argument("--downtime", type=float, default=3600.0,
                        help="seconds without growth = really-down error (latched, never exits; must exceed --silence)")
    parser.add_argument("--sync-file", type=Path, default=None,
                        help="touch-file the sync loop updates on each success; maydown/downtime require it fresh")
    parser.add_argument("--poll", type=float, default=15.0, help="poll interval seconds")
    args = parser.parse_args()

    _load_dotenv()
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    path = args.log.expanduser()
    _info("watchdog", f"watching {path}", silence=args.silence, poll=args.poll)
    _send(webhook, f"watching {args.job} started", title="Watch started", color=0x5865F2)

    offset = path.stat().st_size if path.is_file() else 0
    last_growth = time.monotonic()
    last_progress: str | None = None
    last_event: str | None = None  # "error" | "ok": status of the last reported chunk
    finished = False
    warned = down_alerted = sync_warned = False
    if args.downtime <= args.silence:
        _warn("watchdog", "downtime alert disabled, must exceed silence",
              downtime=args.downtime, silence=args.silence)
        args.downtime = float("inf")
    reported_errors: set[str] = set()
    while True:  # never exits on its own; Ctrl+C to stop
        time.sleep(args.poll)
        chunk, size = _tail_new(path, offset)
        if chunk is not None:
            last_growth = time.monotonic()
            warned = down_alerted = False
            offset = size
            progress, event, done = _ingest_chunk(
                chunk, job=args.job, webhook=webhook,
                error_patterns=args.error_pattern, done_patterns=args.done_pattern,
                reported=reported_errors)
            if progress is not None:
                last_progress = progress
            if event is not None:
                last_event = event
            elif not done:
                if last_event == "error":
                    _info("watchdog", "recovered, job alive again")
                last_event = "ok"
            finished = finished or done
        idle = time.monotonic() - last_growth
        sync_idle, sync_at = _sync_age(args.sync_file)
        sync_kv = {} if sync_idle is None else {"sync_idle": _fmt_age(sync_idle), "sync_at": sync_at}
        sync_fresh = sync_idle is None or sync_idle < args.silence
        state = _heartbeat_state(finished=finished, last_event=last_event)
        if last_progress is not None:
            _info("watchdog", state, idle=_fmt_age(idle), size=size, offset=offset,
                  **sync_kv, last=last_progress[:200])
        else:
            _info("watchdog", state, idle=_fmt_age(idle), size=size, offset=offset,
                  **sync_kv)
        if not sync_fresh:
            if not sync_warned:
                sync_warned = True
                _warn("watchdog", "sync stalled, host state unknown", idle=_fmt_age(idle))
            continue
        sync_warned = False
        if finished or last_event == "error":
            continue  # silence after done/crash is expected; never alert, never exit
        if idle >= args.downtime and not down_alerted:
            down_alerted = True
            warned = True
            _err("watchdog", "host is down", idle=_fmt_age(idle), downtime=_fmt_age(args.downtime))
            _send(webhook, f"{args.job} DOWN {_fmt_age(idle)} - host is down", title="Host down", color=0xFF0000, ping=True)
        elif idle >= args.silence and not warned:
            warned = True
            _warn("watchdog", "host may be down", idle=_fmt_age(idle), silence=_fmt_age(args.silence))
            _send(webhook, f"{args.job} MAYBE-DOWN {_fmt_age(idle)} - host may be down", title="Host may be down", color=0xFFA500, ping=True)


if __name__ == "__main__":
    sys.exit(main())
