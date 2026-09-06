#!/usr/bin/env python3
"""Host-side watchdog for long batch jobs.

Tails a log FILE (local path or synced copy) and reports to a webhook:
error lines matched, a done line, or silence past a deadline (possible
dead host). Runs on the operator machine, independent of the job host,
so it still reports when the job host itself goes away.

Sends fail-open: a dead webhook never stops the watch. Webhook URL comes
from ``DISCORD_WEBHOOK_URL`` env or ``--webhook`` (env wins when both set
is false; explicit flag wins — either way it is never logged).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def _send(webhook: str, text: str) -> None:
    if not webhook:
        return
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"content": text[:1900]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as err:
        print(f"[watchdog] notify failed: {err}", flush=True)


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
    parser.add_argument("--job", default="batch", help="job label used in messages")
    parser.add_argument("--error-pattern", action="append", default=["[error]", "traceback", "error"],
                        help="repeatable substring (case-insensitive) signalling failure")
    parser.add_argument("--done-pattern", action="append", default=["done"],
                        help="repeatable substring signalling clean finish")
    parser.add_argument("--silence", type=float, default=600.0, help="seconds without growth = dead")
    parser.add_argument("--poll", type=float, default=15.0, help="poll interval seconds")
    args = parser.parse_args()

    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    path = args.log
    print(f"[watchdog] watching {path} job={args.job} silence={args.silence}s", flush=True)
    _send(webhook, f"watching {args.job} started")

    offset = path.stat().st_size if path.is_file() else 0
    last_growth = time.monotonic()
    reported_errors: set[str] = set()
    while True:
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
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                continue
            offset = size
            for line in chunk.splitlines():
                hit = _matches_any(line, args.error_pattern)
                if hit is not None and line not in reported_errors:
                    reported_errors.add(line)
                    print(f"[watchdog] error: {line.strip()[:200]}", flush=True)
                    _send(webhook, f"{args.job} ERROR ({hit}): {line.strip()[:500]}")
                    continue
                if _matches_any(line, args.done_pattern) is not None:
                    print(f"[watchdog] done: {line.strip()[:200]}", flush=True)
                    _send(webhook, f"{args.job} done")
                    return 0
        idle = time.monotonic() - last_growth
        if idle >= args.silence:
            print(f"[watchdog] silent {idle:.0f}s >= {args.silence:.0f}s - host may be dead", flush=True)
            _send(webhook, f"{args.job} SILENT {idle:.0f}s - host may be dead")
            return 2


if __name__ == "__main__":
    sys.exit(main())
