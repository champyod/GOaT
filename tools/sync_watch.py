#!/usr/bin/env python3
"""Single-loop batch watcher: fetch a remote log, append it locally, alert.

Each poll is five actions:
  fetch   pull remote tail via CLI subprocess (failures: console + Discord,
          never into the log file, which holds EXACTLY remote bytes)
  append  new bytes to the local log
  ingest  error line -> Discord error; done line -> Discord done (keeps watching)
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

try:
    VERSION = datetime.datetime.fromtimestamp(
        Path(__file__).stat().st_mtime, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
except OSError:
    VERSION = "unknown-mtime"
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


ROLE = "1498710581547634930"


def _send(webhook: str, text: str, title: str, color: int, ping: bool = False) -> None:
    if not webhook:
        return
    try:
        payload = {
            "content": f"<@&{ROLE}>" if ping else "",
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
            headers={"Content-Type": "application/json", "User-Agent": "goat-watch/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as err:
        _say("WARNING", "watch", f"notify failed: {err}")


def _exec_raw(session: str, script: str, timeout: float) -> str | None:
    """Run script on the VM via colab exec; return stdout or None on failure."""
    try:
        proc = subprocess.run(
            ["colab", "exec", "-s", session],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if not ln.startswith("[colab]")]
    return "\n".join(lines) if lines else None


_REAP_SCRIPT = (
    "import datetime, json, os, signal, subprocess, urllib.request\n"
    "def api(path):\n"
    "    try:\n"
    "        with urllib.request.urlopen(f'http://localhost:9000/api/{path}', timeout=10) as r:\n"
    "            return json.load(r)\n"
    "    except Exception:\n"
    "        return None\n"
    "kernels = api('kernels')\n"
    "sessions = api('sessions')\n"
    "if kernels is None or sessions is None:\n"
    "    print('REAPED -1 API-DOWN')\n"
    "else:\n"
    "    busy = set()\n"
    "    for s in sessions:\n"
    "        kid = (s.get('kernel') or {}).get('id')\n"
    "        if kid:\n"
    "            busy.add(kid)\n"
    "    now = datetime.datetime.now(datetime.timezone.utc)\n"
    "    killable = set()\n"
    "    for k in kernels:\n"
    "        kid = k.get('id')\n"
    "        if not kid or kid in busy:\n"
    "            continue\n"
    "        if (k.get('execution_state') or '') != 'idle':\n"
    "            continue\n"
    "        try:\n"
    "            last = datetime.datetime.fromisoformat(k['last_activity'])\n"
    "        except (KeyError, ValueError):\n"
    "            continue\n"
    "        if (now - last).total_seconds() > 300:\n"
    "            killable.add(kid)\n"
    "    rows = subprocess.run(['ps', '-eo', 'pid,args'], capture_output=True, text=True).stdout.splitlines()\n"
    "    mine = os.getpid()\n"
    "    killed = 0\n"
    "    for ln in rows[1:]:\n"
    "        parts = ln.split(None, 1)\n"
    "        if len(parts) != 2:\n"
    "            continue\n"
    "        toks = parts[1].split()\n"
    "        if not any(toks[i] == '-m' and toks[i + 1] == 'colab_kernel_launcher'\n"
    "                   for i in range(len(toks) - 1)):\n"
    "            continue\n"
    "        import re\n"
    "        m = re.search(r'kernel-([0-9a-f-]{36})\\.json', parts[1])\n"
    "        if not m or m.group(1) not in killable:\n"
    "            continue\n"
    "        try:\n"
    "            pid = int(parts[0])\n"
    "        except ValueError:\n"
    "            continue\n"
    "        if pid == mine:\n"
    "            continue\n"
    "        kid = m.group(1)\n"
    "        try:\n"
    "            req = urllib.request.Request(f'http://localhost:9000/api/kernels/{kid}', method='DELETE')\n"
    "            with urllib.request.urlopen(req, timeout=10):\n"
    "                pass\n"
    "            killed += 1\n"
    "        except Exception:\n"
    "            try:\n"
    "                os.kill(pid, signal.SIGKILL)\n"
    "                killed += 1\n"
    "            except OSError:\n"
    "                pass\n"
    "    print(f'REAPED {killed} IDLE-ORPHANS')\n"
)


def _reap_kernels(session: str, timeout: float) -> str:
    """Kill stale colab kernels on the VM, keep 2 newest (incl. the runner)."""
    out = _exec_raw(session, _REAP_SCRIPT, timeout)
    if out is None:
        return "reap exec failed"
    for ln in out.splitlines():
        if ln.startswith("REAPED"):
            return ln
    return "reap bad protocol: " + out[:200]


def _reset_binding(session: str) -> str:
    """Clear stored kernel/session ids so the next exec POSTs one fresh
    kernel. Best-effort: without colab_cli importable there is nothing to do."""
    try:
        from colab_cli.common import state as _cli_state
    except ImportError:
        return "no colab_cli, skip"
    try:
        s = _cli_state.store.get(session)
        if s is None:
            return "no stored session"
        s.kernel_id = None
        s.session_id = None
        _cli_state.store.add(s)
        return "binding cleared, next exec POSTs fresh"
    except Exception as err:
        return f"reset failed: {err}"


def _fetch(session: str, vm_log: str, offset: int, timeout: float) -> tuple[str | None, int | None, str]:
    """(data, remote_size, status). Only bytes past offset are returned, so the
    local log holds exactly remote bytes with no duplicates.

    status: "ok" | "rotated" (remote smaller than offset) | "absent:..." (binding
    works, file not there yet) |colab failure text. data/remote_size are None
    unless status is "ok".
    """
    script = (
        "import os\n"
        f"path = {vm_log!r}\n"
        f"off = {offset:d}\n"
        "try:\n"
        "    size = os.path.getsize(path)\n"
        "except OSError as e:\n"
        "    print(f'REMOTE-MISS {e}')\n"
        "else:\n"
        "    if size < off:\n"
        "        print('REMOTE-ROTATED')\n"
        "    else:\n"
        "        try:\n"
        "            with open(path, 'rb') as f:\n"
        "                f.seek(off)\n"
        "                data = f.read()\n"
        "        except OSError as e:\n"
        "            print(f'REMOTE-MISS {e}')\n"
        "        else:\n"
        "            print(f'REMOTE-SIZE {size}')\n"
        "            print(data.decode('utf-8', 'replace'), end='')\n"
    )
    try:
        proc = subprocess.run(
            ["colab", "exec", "-s", session],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, None, f"exec failed: {err}"
    out = proc.stdout.decode("utf-8", "replace")
    lines = [ln for ln in out.splitlines() if not ln.startswith("[colab]")]
    if proc.returncode != 0 or not lines:
        err_text = proc.stderr.decode("utf-8", "replace").strip()
        return None, None, err_text or f"exit {proc.returncode}"
    first = lines[0]
    if first.startswith("REMOTE-MISS"):
        return None, None, "absent:" + first[len("REMOTE-MISS "):][:200]
    if first == "REMOTE-ROTATED":
        return None, None, "rotated"
    match = re.match(r"REMOTE-SIZE (\d+)$", first)
    if not match:
        return None, None, "bad protocol: " + first[:200]
    return "\n".join(lines[1:]), int(match.group(1)), "ok"


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
            val = val.strip().strip("'").strip('"')
            if key and key not in _os.environ:
                _os.environ[key] = val


def _read_int(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Fetch a remote batch log locally and watch it.")
    parser.add_argument("--vm-log", required=True, help="remote log path on the job host")
    parser.add_argument("--session", default="goat", help="session name for the exec CLI")
    parser.add_argument("--out", type=Path, default=Path("~/synced/goat.log"), help="local copy")
    parser.add_argument("--webhook", default="", help="webhook URL (or DISCORD_WEBHOOK_URL env)")
    parser.add_argument("--job", default="batch", help="job label in messages")
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
                        default=["done", "finished", "complete", "completed",
                                 "success", "training complete", "training done", "all done"],
                        help="exact lines (case-insensitive) signalling clean finish")
    parser.add_argument("--silence", type=float, default=600.0, help="stale seconds before warn")
    parser.add_argument("--downtime", type=float, default=3600.0, help="stale seconds before error")
    parser.add_argument("--poll", type=float, default=15.0, help="poll interval seconds")
    parser.add_argument("--reap-every", type=int, default=20,
                        help="run kernel reaper every N polls (0 disables)")
    args = parser.parse_args()

    _load_dotenv()
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    _say("INFO", "watch", f"starting version={VERSION} job={args.job} vm_log={args.vm_log} out={out}")
    _send(webhook, f"watching {args.job} started (v{VERSION})", "Watch started", 0x5865F2)

    offset_path = Path(str(out) + ".offset")
    offset = _read_int(offset_path)
    file_size = out.stat().st_size if out.is_file() else 0
    start = time.monotonic()
    last_content: float | None = None
    last_action: str | None = None
    last_event: str | None = None  # "error" | "ok"
    finished = False
    last_ok = start
    last_growth = start
    polls = 0
    consec_fail = 0
    fetch_failed = vm_warned = vm_down = absent_warned = False
    reported: set[str] = set()
    while True:
        time.sleep(args.poll)
        polls += 1
        # reap: every exec spawns a VM kernel that is never culled; kill
        # stale ones, keep 2 newest. The reaper exec itself adds one.
        if args.reap_every > 0 and polls % args.reap_every == 0:
            _say("INFO", "watch", f"reap: {_reap_kernels(args.session, timeout=args.poll * 4)}")
        # fetch: only new bytes past offset; failures known directly,
        # never written into the log, which holds EXACTLY remote bytes.
        data, remote_size, status = _fetch(args.session, args.vm_log, offset, timeout=args.poll * 4)
        now_mono = time.monotonic()
        now_wall = time.time()
        if status == "ok":
            last_ok = now_mono
            consec_fail = 0
            if fetch_failed:
                _say("INFO", "watch", "fetch recovered")
                fetch_failed = False
            absent_warned = False
            if remote_size is not None:
                offset = remote_size
                offset_path.write_text(str(offset), encoding="utf-8")
            if data:
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write(data if data.endswith("\n") else data + "\n")
                file_size = out.stat().st_size
                last_growth = now_mono
                ts = _content_time(data)
                if ts is not None:
                    last_content = ts
                chunk_has_error = False
                for line in data.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    last_action = stripped
                    lowered = stripped.lower()
                    if any(p.lower() in lowered for p in args.error_pattern) and stripped not in reported:
                        reported.add(stripped)
                        chunk_has_error = True
                        last_event = "error"
                        _say("ERROR", "watch", stripped[:200])
                        _send(webhook, f"{args.job} ERROR: {stripped[:1000]}", "Job ERROR", 0xFF0000, True)
                        continue
                    # Exact-line match only: "synthetic done" (a phase) must not
                    # count as done while the job continues.
                    if any(stripped.lower() == p.lower() for p in args.done_pattern):
                        finished = True
                        _say("INFO", "watch", f"done: {stripped[:200]}")
                        _send(webhook, f"{args.job} done", "Job done", 0x00FF00, True)
                if not chunk_has_error:
                    if last_event == "error":
                        _say("INFO", "watch", "recovered, job alive again")
                    last_event = "ok"
        elif status == "rotated":
            last_ok = now_mono
            consec_fail = 0
            _say("WARNING", "watch", "vm log rotated, restarting local copy")
            out.write_bytes(b"")
            file_size = 0
            offset = 0
            offset_path.write_text("0", encoding="utf-8")
        elif status.startswith("absent:"):
            last_ok = now_mono  # binding works; the file just isn't there yet
            consec_fail = 0
            if not absent_warned:
                absent_warned = True
                _say("WARNING", "watch", f"vm log absent ({status[7:][:150]})")
        elif not fetch_failed:
            fetch_failed = True
            _say("WARNING", "watch", f"fetch failed: {status}")
            _send(webhook, f"{args.job} fetch failed: {status[:500]}",
                  "Fetch failed", 0xFFA500, True)
        else:
            consec_fail += 1
            # Poisoned stored kernel binding fails every poll the same way;
            # every 3rd consecutive failure, drop it so one fresh POST happens.
            if consec_fail % 3 == 0:
                _say("INFO", "watch", f"binding reset: {_reset_binding(args.session)}")
        # ages: vm from content timestamps, else growth, else last good fetch.
        candidates = [now_mono - last_growth]
        if last_content is not None:
            candidates.append(now_wall - last_content)
        vm_age = min(candidates)
        fetch_age = now_mono - last_ok
        action = f" last={(last_action[:160] if last_action else '-')}"
        state = "healthy"
        if finished:
            state = "done"
        elif last_event == "error":
            state = "stuck-error"
        _say("INFO", "watch",
             f"{state} vm={_age(vm_age)} fetch={_age(fetch_age)} size={file_size}{action}")
        if finished or last_event == "error":
            continue  # silence after done/crash is expected; never alert, never exit
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
