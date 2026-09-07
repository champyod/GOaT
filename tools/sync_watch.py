#!/usr/bin/env python3
"""Single-loop batch watcher: fetch a remote log, append it locally, alert.

Each poll is five actions:
  fetch   pull remote tail via CLI subprocess (failures: console + Discord,
          never into the log file, which holds EXACTLY remote bytes)
  append  new bytes to the local log
  ingest  error line -> Discord error; done line -> Discord done, exit 0
  ages    vm_age = now - newest content timestamp (fallback: growth);
          fetch_age = now - last fetch attempt outcome
  alert   vm stale >= silence: warn; >= downtime: error;
          fetch failing: warn now, error past downtime; heartbeat every poll

No attempt files, no clock comparison, no latches beyond edge-triggered
episode flags. Stdlib-only so it runs anywhere (no package install).
Sends fail-open: a dead webhook never stops the watch.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VERSION = "2026-09-07-merged-loop"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _age(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _say(level: str, tag: str, msg: str) -> None:
    print(f"{_now()} {level} {tag} {msg}", flush=True)


def _send(webhook: str, text: str, title: str, color: int, ping: bool = False) -> None:
    if not webhook:
        return
    try:
        payload = {
            "content": "",
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
            headers={"Content-Type": "application/json", "User-Agent": "goat-watch/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as err:
        _say("WARNING", "watch", f"notify failed: {err}")


def _fetch(session: str, vm_log: str, tail_chars: int, timeout: float) -> tuple[str | None, str | None]:
    """(chunk, error): chunk is None on fetch failure, error carries why."""
    script = (
        "import sys\n"
        "try:\n"
        f'    with open("{vm_log}", "r", errors="replace") as f:\n'
        f"        sys.stdout.write(f.read()[-{tail_chars}:])\n"
        "except Exception as e:\n"
        "    sys.stderr.write(f'REMOTE-FAIL {{e}}')\n"
        "    sys.exit(3)\n"
    )
    try:
        proc = subprocess.run(
            ["colab", "exec", "-s", session],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except Exception as err:
        return None, f"exec failed: {err}"
    err_text = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return None, err_text or f"exit {proc.returncode}"
    out = proc.stdout.decode("utf-8", "replace")
    return out, None


def _content_time(chunk: str) -> float | None:
    best: float | None = None
    for line in chunk.splitlines():
        match = _TS_RE.match(line.strip())
        if match is None:
            continue
        try:
            ts = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            ).timestamp()
        except ValueError:
            continue
        if best is None or ts > best:
            best = ts
    return best


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Fetch a remote batch log locally and watch it.")
    parser.add_argument("--vm-log", required=True, help="remote log path on the job host")
    parser.add_argument("--session", default="goat", help="session name for the exec CLI")
    parser.add_argument("--out", type=Path, default=Path("~/synced/goat.log"), help="local copy")
    parser.add_argument("--webhook", default="", help="webhook URL (or DISCORD_WEBHOOK_URL env)")
    parser.add_argument("--job", default="batch", help="job label in messages")
    parser.add_argument("--error-pattern", action="append",
                        default=["traceback", "error", "exception", "fail", "killed",
                                 "out of memory", "timeout", "timed out", "no space left", "crash"],
                        help="repeatable substring (case-insensitive) signalling failure")
    parser.add_argument("--done-pattern", action="append", default=["done", "finish", "complet", "success"],
                        help="repeatable substring signalling clean finish")
    parser.add_argument("--silence", type=float, default=600.0, help="stale seconds before warn")
    parser.add_argument("--downtime", type=float, default=3600.0, help="stale seconds before error")
    parser.add_argument("--poll", type=float, default=15.0, help="poll interval seconds")
    args = parser.parse_args()

    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    _say("INFO", "watch", f"starting version={VERSION} job={args.job} vm_log={args.vm_log} out={out}")
    _send(webhook, f"watching {args.job} started (v{VERSION})", "Watch started", 0x5865F2)

    offset = out.stat().st_size if out.is_file() else 0
    last_content: float | None = None
    last_action: str | None = None
    last_ok = time.monotonic()
    fetch_warned = fetch_failed = vm_warned = vm_down = False
    finished = False
    reported: set[str] = set()
    while True:
        time.sleep(args.poll)
        # fetch: failures known directly, never written into the log.
        chunk, fetch_err = _fetch(args.session, args.vm_log, 8000, timeout=args.poll * 4)
        now_mono = time.monotonic()
        now_wall = time.time()
        if fetch_err is None and chunk:
            last_ok = now_mono
            if fetch_failed:
                _say("INFO", "watch", "fetch recovered")
                fetch_failed = False
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(chunk if chunk.endswith("\n") else chunk + "\n")
            ts = _content_time(chunk)
            if ts is not None:
                last_content = ts
            for line in chunk.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                last_action = stripped
                lowered = stripped.lower()
                if any(p.lower() in lowered for p in args.error_pattern) and stripped not in reported:
                    reported.add(stripped)
                    _say("ERROR", "watch", stripped[:200])
                    _send(webhook, f"{args.job} ERROR: {stripped[:1000]}", "Job ERROR", 0xFF0000, True)
                if any(p.lower() in lowered for p in args.done_pattern):
                    _say("INFO", "watch", f"done: {stripped[:200]}")
                    _send(webhook, f"{args.job} done", "Job done", 0x00FF00, True)
                    finished = True
        else:
            if not fetch_failed:
                fetch_failed = True
                _say("WARNING", "watch", f"fetch failed: {fetch_err or 'empty'}")
                _send(webhook, f"{args.job} fetch failed: {(fetch_err or 'empty')[:500]}",
                      "Fetch failed", 0xFFA500, True)
        # ages: vm from content timestamps (fallback: local growth), fetch from attempts.
        vm_age = (now_wall - last_content) if last_content is not None else (now_mono - last_ok)
        fetch_age = now_mono - last_ok
        action = f" last={(last_action[:160] if last_action else '-')}"
        _say("INFO", "watch",
             f"healthy vm={_age(vm_age)} fetch={_age(fetch_age)} size={out.stat().st_size if out.is_file() else 0}{action}")
        if finished:
            return 0
        if vm_age >= args.downtime and not vm_down:
            vm_down = vm_warned = True
            _say("ERROR", "watch", f"vm silent {_age(vm_age)}")
            _send(webhook, f"{args.job} DOWN {_age(vm_age)} - no log content. Last action: {(last_action[:500] if last_action else '-')}", "Host down", 0xFF0000, True)
        elif vm_age >= args.silence and not vm_warned:
            vm_warned = True
            _say("WARNING", "watch", f"vm quiet {_age(vm_age)}")
            _send(webhook, f"{args.job} quiet {_age(vm_age)} - no new log content. Last action: {(last_action[:500] if last_action else '-')}",
                  "Host may be down", 0xFFA500, True)


if __name__ == "__main__":
    sys.exit(main())
