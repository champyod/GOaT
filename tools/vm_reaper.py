#!/usr/bin/env python3
"""VM-side kernel janitor: DELETE idle sessionless kernels via the local API.

Runs ON the VM (nohup), needs no Pi, no exec, no kernel of its own, so it
keeps working exactly when the Pi watcher is blind. Same kill rules as the
watcher's reaper: exact `python -m colab_kernel_launcher` match, no session,
idle, last activity >300s. Shutdown goes through API DELETE so the server
never holds stale mappings (raw SIGKILL is fallback only).

Usage on the VM:
    nohup python3 /content/GOaT/tools/vm_reaper.py --interval 300 > /tmp/vm_reaper.log 2>&1 &
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _say(msg: str) -> None:
    print(f"{_now()} reaper {msg}", flush=True)


def _hosts() -> list[str]:
    hs = ["127.0.0.1"]
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip not in hs:
            hs.append(ip)
    except OSError:
        pass
    return hs


def _call(method: str, path: str, timeout: float = 10.0) -> tuple[bytes | None, str]:
    err = ""
    for host in _hosts():
        try:
            req = urllib.request.Request(f"http://{host}:9000/api/{path}", method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), ""
        except Exception as exc:  # noqa: BLE001 - report, never raise
            err = type(exc).__name__
    return None, err


def _api(path: str) -> tuple[Any, str]:
    data, err = _call("GET", path)
    if data is None:
        return None, err
    try:
        return json.loads(data), ""
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def _reap_once() -> str:
    kernels, kerr = _api("kernels")
    sessions, serr = _api("sessions")
    if kernels is None or sessions is None:
        return f"API-DOWN kernels={kerr} sessions={serr}"
    busy: set[str] = set()
    for sess in sessions:
        kid = (sess.get("kernel") or {}).get("id")
        if kid:
            busy.add(kid)
    now = datetime.datetime.now(datetime.timezone.utc)
    killable: set[str] = set()
    for kernel in kernels:
        kid = kernel.get("id")
        if not kid or kid in busy:
            continue
        if (kernel.get("execution_state") or "") != "idle":
            continue
        try:
            last = datetime.datetime.fromisoformat(kernel["last_activity"])
        except (KeyError, ValueError):
            continue
        if (now - last).total_seconds() > 300:
            killable.add(kid)
    if not killable:
        return f"ok kernels={len(kernels)} sessions={len(sessions)}"
    rows = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout.splitlines()
    killed = 0
    for line in rows[1:]:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        toks = parts[1].split()
        if not any(toks[i] == "-m" and toks[i + 1] == "colab_kernel_launcher"
                   for i in range(len(toks) - 1)):
            continue
        match = re.search(r"kernel-([0-9a-f-]{36})\.json", parts[1])
        if not match or match.group(1) not in killable:
            continue
        kid = match.group(1)
        _, derr = _call("DELETE", f"kernels/{kid}")
        if derr == "":
            killed += 1
            continue
        try:
            os.kill(int(parts[0]), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError):
            pass
    return f"REAPED {killed} of {len(killable)} idle-orphans"


def main() -> int:
    parser = argparse.ArgumentParser(description="VM-local idle-kernel reaper.")
    parser.add_argument("--interval", type=float, default=300.0, help="seconds between sweeps")
    args = parser.parse_args()
    _say(f"starting interval={args.interval}s")
    while True:
        try:
            _say(_reap_once())
        except Exception as exc:  # noqa: BLE001 - janitor never dies
            _say(f"sweep failed: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
