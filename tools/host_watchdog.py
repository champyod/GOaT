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
    python tools/host_watchdog.py --log ~/synced/train.log --job goat-train \\
        --silence 900 --downtime 3600 --poll 15
    # per-poll stdout: INFO watchdog <state> idle=.. size=.. offset=.. job=.. [last=..]
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


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Watch a batch log file and report to a webhook.")
    parser.add_argument("--log", type=Path, required=True, help="log file to tail")
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
    parser.add_argument("--poll", type=float, default=15.0, help="poll interval seconds")
    args = parser.parse_args()

    _load_dotenv()
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    path = args.log
    _info("watchdog", f"watching {path}", silence=args.silence, poll=args.poll)
    _send(webhook, f"watching {args.job} started", title="Watch started", color=0x5865F2)

    offset = path.stat().st_size if path.is_file() else 0
    last_growth = time.monotonic()
    last_progress: str | None = None
    last_event: str | None = None  # "error" | "ok": status of the last reported chunk
    finished = False
    warned = False
    down_alerted = False
    if args.downtime <= args.silence:
        _warn("watchdog", "downtime alert disabled, must exceed silence",
              downtime=args.downtime, silence=args.silence)
        args.downtime = float("inf")
    reported_errors: set[str] = set()
    while True:  # never exits on its own; Ctrl+C to stop
        time.sleep(args.poll)
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < offset:
            offset = 0  # rotated/truncated: reread from start
        if size > offset:
            last_growth = time.monotonic()
            warned = False
            down_alerted = False
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                continue
            offset = size
            chunk_has_error = False
            for line in chunk.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                progress = re.sub(r"^\S+ (?:INFO|WARNING|ERROR) ", "", stripped).strip()
                last_progress = progress or stripped
                hit = _matches_any(line, args.error_pattern)
                if hit is not None and line not in reported_errors:
                    reported_errors.add(line)
                    chunk_has_error = True
                    last_event = "error"
                    _err("watchdog", line.strip()[:200], match=hit)
                    _send(webhook, f"{args.job} ERROR ({hit}): {line.strip()[:1000]}", title="Training ERROR", color=0xFF0000, ping=True)
                    continue
                if _matches_any(line, args.done_pattern) is not None:
                    finished = True
                    _info("watchdog", line.strip()[:200], state="done")
                    _send(webhook, f"{args.job} done", title="Training done", color=0x00FF00, ping=True)
            if not chunk_has_error:
                if last_event == "error":
                    _info("watchdog", "recovered, job alive again")
                last_event = "ok"
        idle = time.monotonic() - last_growth
        if last_progress is not None:
            state = "healthy"
            if finished:
                state = "done, watching"
            elif last_event == "error":
                state = "stuck at error, waiting for recovery"
            _info("watchdog", state, idle=f"{idle:.0f}s", size=size, offset=offset,
                  last=last_progress[:200])
        else:
            _info("watchdog", "healthy", idle=f"{idle:.0f}s", size=size, offset=offset)
        if finished or last_event == "error":
            continue  # silence after done/crash is expected; never alert, never exit
        if idle >= args.downtime and not down_alerted:
            down_alerted = True
            warned = True
            _err("watchdog", "host is down", idle=f"{idle:.0f}s", downtime=args.downtime)
            _send(webhook, f"{args.job} DOWN {idle:.0f}s - host is down", title="Host down", color=0xFF0000, ping=True)
        elif idle >= args.silence and not warned:
            warned = True
            _warn("watchdog", "host may be down", idle=f"{idle:.0f}s", silence=args.silence)
            _send(webhook, f"{args.job} MAYBE-DOWN {idle:.0f}s - host may be down", title="Host may be down", color=0xFFA500, ping=True)


if __name__ == "__main__":
    sys.exit(main())
