#!/usr/bin/env python3
"""RAM measurement of a running GOaT app over two explicitly declared windows.

Each phase reports three figures from three kernel sources, because none is
trustworthy alone: polled RSS from `psutil`, the kernel's own `VmHWM` high-water
mark, and the cgroup v2 `memory.peak` counter. `VmHWM` is exact but monotonic, so
it cannot be scoped to one phase; `memory.peak` is resettable, so it can, but it
counts every process in the cgroup rather than `--pid` alone; polled RSS is neither
and exists to cross-check the other two. Every figure carries the method that
produced it, so no number can be quoted without its provenance.

`--pid` is mandatory. The previous default sampled this script, so a run without it
reported the sampler's own memory under a field name that read like the app's.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import psutil

from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, write_json

PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
BYTES_PER_MB = 1_000_000
MB_UNIT = "MB = 1e6 bytes"
PARTIAL_EVERY = 60
CGROUP_PEAK_METHOD = ("cgroup v2 memory.peak, reset to 0 at this window's open and read at "
                      "its close; counts the whole cgroup, so it may exceed this pid")

#: `Name: <n> kB` counters in `/proc/<pid>/status`. `RssAnon`/`RssFile`/`RssShmem` are
#: Linux 4.5+ and sum to `VmRSS`; the sum is checked rather than assumed, so a kernel
#: that omits them shows as a null split instead of a wrong one.
STATUS_KEYS = frozenset({"VmPeak", "VmHWM", "VmRSS", "RssAnon", "RssFile", "RssShmem"})

#: Reasons a figure read at a window's close is null. An empty status dict means the file
#: was unreadable then - the process was gone, or the kernel denied it - which is not the
#: same as counters that read zero, so nothing derived from it may sit null unlabelled.
NEVER_CLOSED_REASON = "window never closed - /proc/<pid>/status was not read at its close"
CLOSE_STATUS_UNREADABLE_REASON = ("/proc/<pid>/status could not be read at this window's close "
                                  "- no such process, or no permission - so nothing measured "
                                  "from it exists for this window")
NO_SPLIT_REASON = ("this kernel's /proc/<pid>/status carries no RssAnon, RssFile or VmRSS "
                   "counter")
NO_VM_HWM_REASON = "this kernel's /proc/<pid>/status carries no VmHWM counter"


class Window(NamedTuple):
    """A declared measurement window.

    Nothing here is inferred from sample order: the operator states where each phase
    starts and how long it lasts, so "idle" means an interval they chose rather than
    whatever the trailing fraction of a run happened to contain.
    """

    name: str
    start_s: float
    duration_s: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    def contains(self, t_s: float) -> bool:
        return self.start_s <= t_s <= self.end_s


def _proc_status(pid: int) -> dict[str, int]:
    """Selected byte counters from `/proc/<pid>/status`; empty when unreadable."""
    try:
        text = (PROC_ROOT / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        parts = rest.split()
        if sep and key in STATUS_KEYS and len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit():
            fields[key] = int(parts[0]) * 1024
    return fields


def _anon_file_split(status: dict[str, int]) -> dict:
    """Anonymous vs file-backed resident bytes, and whether the kernel's own sum of
    the parts agrees with `VmRSS` — a null split and a silent mismatch look identical
    in a report, and only one of them is honest."""
    anon, file_backed = status.get("RssAnon"), status.get("RssFile")
    shmem, rss = status.get("RssShmem"), status.get("VmRSS")
    if anon is None or file_backed is None or rss is None:
        return {"available": False, "anon_bytes": None, "file_bytes": None,
                "shmem_bytes": shmem, "rss_bytes": rss, "parts_sum_bytes": None,
                "residual_bytes": None}
    parts_sum = anon + file_backed + (shmem or 0)
    return {"available": True, "anon_bytes": anon, "file_bytes": file_backed,
            "shmem_bytes": shmem, "rss_bytes": rss, "parts_sum_bytes": parts_sum,
            "residual_bytes": rss - parts_sum}


def _cgroup_dir(pid: int) -> Path | None:
    """The cgroup v2 directory holding `--pid`, or None when off cgroup v2."""
    try:
        lines = (PROC_ROOT / str(pid) / "cgroup").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and controllers == "":
            candidate = CGROUP_ROOT / path.lstrip("/")
            return candidate if candidate.is_dir() else None
    return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def read_cgroup_peak(pid: int) -> dict:
    """cgroup v2 `memory.peak` / `memory.current` for the pid's cgroup, plus whether
    the counter could be reset here."""
    directory = _cgroup_dir(pid)
    if directory is None:
        return {"available": False, "reason": "no cgroup v2 memory directory for this pid",
                "cgroup_path": None, "peak_bytes": None, "current_bytes": None,
                "resettable": False}
    peak_path = directory / "memory.peak"
    peak = _read_int(peak_path)
    return {"available": peak is not None,
            "reason": None if peak is not None else f"no readable memory.peak in {directory}",
            "cgroup_path": str(directory), "peak_bytes": peak,
            "current_bytes": _read_int(directory / "memory.current"),
            "resettable": os.access(peak_path, os.W_OK)}


def reset_cgroup_peak(pid: int) -> bool:
    """Zero `memory.peak` so the next read covers only the phase that follows.

    False when there is no writable `memory.peak`, the normal case on kernels older
    than 5.19 and on any cgroup v1 host. The caller then records the phase as
    unavailable rather than reporting a peak that predates it.
    """
    directory = _cgroup_dir(pid)
    if directory is None:
        return False
    try:
        (directory / "memory.peak").write_text("0\n", encoding="ascii")
    except OSError as exc:
        _warn("ram", "cgroup peak not resettable", pid=pid, error=str(exc))
        return False
    return True


def _stats(values: list[int]) -> dict | None:
    """mean/std/max/min in MB, or None for an empty window.

    `std` is the sample standard deviation and stays null for a single sample:
    ddof=1 is undefined there, and printing 0.0 would read as a measured stability
    rather than as an absent measurement.
    """
    if not values:
        return None
    mib = [value / BYTES_PER_MB for value in values]
    return {"n": len(values), "mean_mb": float(np.mean(mib)), "max_mb": float(np.max(mib)),
            "min_mb": float(np.min(mib)),
            "std_mb": float(np.std(mib, ddof=1)) if len(mib) >= 2 else None}


def _cgroup_block(phase: dict) -> dict:
    """The phase's cgroup peak, or the reason there is none.

    A live read is never substituted for a missing one: `memory.peak` counts from its
    last reset, so a value read now would cover every earlier phase and still print
    as this one's peak. That includes the window whose own reset failed - the read
    succeeds there and returns the whole cgroup's history, a real number belonging to
    no window, so it is dropped rather than reported.
    """
    if phase["cgroup"] is not None:
        if phase["cgroup_reset"]:
            return {**phase["cgroup"], "method": CGROUP_PEAK_METHOD, "cgroup_reset": True}
        return {"available": False,
                "reason": "memory.peak could not be reset at this window's open, so the "
                          "value read at its close predates the window and is not this "
                          "phase's peak",
                "cgroup_path": phase["cgroup"].get("cgroup_path"),
                "peak_bytes": None, "current_bytes": None, "resettable": False,
                "read_at_s": phase["cgroup"].get("read_at_s"),
                "method": CGROUP_PEAK_METHOD, "cgroup_reset": False}
    if not phase["opened"]:
        reason = "window never opened - the run ended before it started, no reset happened"
    elif not phase["closed"]:
        reason = "window never closed - the run ended inside it, no read was taken"
    else:
        reason = "no reading recorded for this window"
    return {"available": False, "reason": reason, "peak_bytes": None, "peak_mb": None,
            "cgroup_path": None, "read_at_s": None, "method": CGROUP_PEAK_METHOD,
            "cgroup_reset": phase["cgroup_reset"]}


def _anon_block(phase: dict) -> dict:
    """The anon/file split read at this window's close, or why there is none.

    Read per close rather than once at the end of the run: a single read hands every
    window the process's last state, so idle would report what the process looked like
    when the active window ended. An unavailable split always names its reason, so a
    null one cannot be read downstream as a measured zero.
    """
    status = phase["status_at_close"]
    if status is None:
        return {"available": False, "reason": NEVER_CLOSED_REASON,
                "anon_bytes": None, "file_bytes": None, "shmem_bytes": None,
                "rss_bytes": None, "parts_sum_bytes": None, "residual_bytes": None}
    split = _anon_file_split(status)
    if split["available"]:
        return split
    return {**split,
            "reason": CLOSE_STATUS_UNREADABLE_REASON if not status else NO_SPLIT_REASON}


def _vm_hwm_at_end(phase: dict) -> tuple[float | None, str | None]:
    """This window's own end-of-window `VmHWM` in MB, and why it is null when it is.

    Taken from the read at this window's close, so it belongs to this window rather than
    to the end of the run. A null value is an absent measurement rather than a small one,
    so it carries the reason instead of sitting silent next to a real figure.
    """
    status = phase["status_at_close"]
    if status is None:
        return None, NEVER_CLOSED_REASON
    if "VmHWM" not in status:
        return None, (CLOSE_STATUS_UNREADABLE_REASON if not status else NO_VM_HWM_REASON)
    return status["VmHWM"] / BYTES_PER_MB, None


def _window_block(samples: list[dict], window: Window, interval_s: float,
                  phase: dict) -> dict:
    """One phase: three figures, each labelled with the method that produced it."""
    inside = [s for s in samples if window.contains(s["t_s"])]
    rss = [s["rss_bytes"] for s in inside if s["rss_bytes"] is not None]
    hwm = [s["vm_hwm_bytes"] for s in inside if s["vm_hwm_bytes"] is not None]
    cgroup = _cgroup_block(phase)
    hwm_at_end_mb, hwm_at_end_reason = _vm_hwm_at_end(phase)
    return {
        "window": {"start_s": window.start_s, "duration_s": window.duration_s,
                   "end_s": window.end_s},
        "rss_sampled": {
            "stats_mb": _stats(rss),
            "method": f"psutil.Process(pid).memory_info().rss polled every {interval_s} s; "
                      "sees only the instants it sampled",
        },
        "vm_hwm": {
            "max_observed_mb": max(hwm) / BYTES_PER_MB if hwm else None,
            "value_at_window_end_mb": hwm_at_end_mb,
            "value_at_window_end_reason": hwm_at_end_reason,
            "method": "/proc/<pid>/status VmHWM - kernel high-water mark since process "
                      "start, max over samples inside the window, plus the value read at "
                      "the window's close; monotonic and not resettable, so it also carries "
                      "any pre-window peak",
        },
        "cgroup_peak": {**cgroup, "peak_mb": (cgroup["peak_bytes"] / BYTES_PER_MB)
                        if cgroup["peak_bytes"] is not None else None},
        "anon_vs_file": _anon_block(phase),
        "anon_vs_file_method": "/proc/<pid>/status RssAnon / RssFile / RssShmem at window "
                               "close, cross-checked against VmRSS",
    }


def _advance_windows(pid: int, now_s: float, windows: list[Window],
                     phases: dict[str, dict]) -> None:
    """Move every window's state up to `now_s`: reset `memory.peak` on first entry,
    read it on the first sample past the end, and read `/proc/<pid>/status` at that
    same close so the anon/file split and the window-end `VmHWM` belong to this
    window rather than to the end of the run.

    Resetting per window rather than once at startup is what makes each phase's peak
    cover that phase and not the run. Reading at the close rather than at the end of
    the run is what keeps the figure inside the window: up to one extra poll interval
    of later activity is included, and `read_at_s` records exactly how late it was.
    """
    for window in windows:
        phase = phases[window.name]
        if not phase["opened"] and window.contains(now_s):
            phase["opened"], phase["cgroup_reset"] = True, reset_cgroup_peak(pid)
            _info("ram", "window open", name=window.name, at_s=round(now_s, 3),
                  cgroup_peak_reset=phase["cgroup_reset"])
        if phase["opened"] and not phase["closed"] and now_s > window.end_s:
            phase["closed"] = True
            phase["status_at_close"] = _proc_status(pid)
            phase["cgroup"] = {**read_cgroup_peak(pid), "read_at_s": now_s}
            _info("ram", "window close", name=window.name, at_s=round(now_s, 3),
                  vm_hwm_at_close_mb=(phase["status_at_close"].get("VmHWM", 0)
                                     / BYTES_PER_MB),
                  cgroup_peak_bytes=phase["cgroup"]["peak_bytes"])


def _collect_samples(pid: int, args: argparse.Namespace, windows: list[Window],
                     phases: dict[str, dict], proc: psutil.Process) -> list[dict]:
    """Poll RSS and VmHWM until the last window has closed, checkpointing as it goes."""
    samples: list[dict] = []
    run_end_s = max(w.end_s for w in windows) + args.interval
    started_at = time.monotonic()
    _info("ram", "sampling", pid=pid, interval_s=args.interval, run_end_s=run_end_s,
          windows=[w.name for w in windows], unit=MB_UNIT)
    while time.monotonic() - started_at <= run_end_s:
        now_s = time.monotonic() - started_at
        try:
            rss_bytes = proc.memory_info().rss
        except psutil.NoSuchProcess:
            _warn("ram", "target exited", pid=pid, samples=len(samples))
            break
        samples.append({"t_s": now_s, "rss_bytes": rss_bytes,
                        "vm_hwm_bytes": _proc_status(pid).get("VmHWM")})
        _advance_windows(pid, now_s, windows, phases)
        if len(samples) % PARTIAL_EVERY == 0:
            write_json(args.output, {"pid": pid, "partial": True, "n": len(samples),
                                     "last_mb": rss_bytes / BYTES_PER_MB,
                                     "note": "run incomplete - not a measurement"})
        time.sleep(args.interval)
    _advance_windows(pid, time.monotonic() - started_at, windows, phases)
    return samples


def _build_summary(args: argparse.Namespace, windows: list[Window], phases: dict[str, dict],
                   samples: list[dict], pid: int) -> dict:
    """The artefact: every figure with its method, and no figure without one."""
    return {
        "pid": pid,
        "interval_s": args.interval,
        "unit": MB_UNIT,
        "method": {
            "rss_sampled": "psutil memory_info().rss, polled",
            "vm_hwm": "/proc/<pid>/status VmHWM, monotonic since process start",
            "cgroup_peak": CGROUP_PEAK_METHOD,
            "anon_vs_file": "/proc/<pid>/status RssAnon + RssFile + RssShmem vs VmRSS",
        },
        "windows": {w.name: _window_block(samples, w, args.interval, phases[w.name])
                    for w in windows},
        "whole_run": {
            "stats_mb": _stats([s["rss_bytes"] for s in samples]),
            "samples": len(samples),
            "method": "every polled sample - both windows and the gap between them",
        },
        "phases": {name: {k: phase[k] for k in ("opened", "closed", "cgroup_reset")}
                   for name, phase in phases.items()},
        "samples": samples,
    }


def _first_overlap(windows: list[Window]) -> tuple[str, str] | None:
    """The first pair of windows sharing an instant, or None when all are disjoint.

    Two windows that share an instant feed the same sample into both phases, so each
    phase reports part of the other's memory and neither figure means anything. Windows
    that merely touch - one ending exactly where the next opens - do not overlap: the
    declared start of the later window is what an operator means by the boundary.
    """
    for index, window in enumerate(windows):
        for later in windows[index + 1:]:
            if window.start_s < later.end_s and later.start_s < window.end_s:
                return window.name, later.name
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RSS / VmHWM / cgroup-peak sampler.")
    parser.add_argument("--pid", type=int, required=True,
                        help="process to monitor (required: without it the sampler "
                             "would measure itself)")
    parser.add_argument("--idle-start", type=float, default=0.0,
                        help="seconds after start when the idle window opens")
    parser.add_argument("--idle-duration", type=float, default=60.0)
    parser.add_argument("--active-start", type=float, default=60.0,
                        help="seconds after start when the active window opens")
    parser.add_argument("--active-duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=Path("results/ram.json"))
    return parser.parse_args()


@log_call
def main() -> None:
    args = _parse_args()
    if args.idle_duration <= 0 or args.active_duration <= 0:
        raise ValueError("window durations must be positive")
    if args.interval <= 0:
        raise ValueError("--interval must be positive")
    windows = [Window("idle", args.idle_start, args.idle_duration),
               Window("active", args.active_start, args.active_duration)]
    overlap = _first_overlap(windows)
    if overlap is not None:
        raise ValueError(f"windows {overlap[0]!r} and {overlap[1]!r} overlap; a sample in the "
                         f"shared interval would be counted in both phases")
    phases = {w.name: {"opened": False, "closed": False, "cgroup": None,
                       "cgroup_reset": None, "status_at_close": None} for w in windows}
    if not _proc_status(args.pid):
        raise RuntimeError(f"cannot read /proc/{args.pid}/status - no permission, "
                           f"or no such process")
    summary = _build_summary(args, windows, phases,
                             _collect_samples(args.pid, args, windows, phases,
                                              psutil.Process(args.pid)), args.pid)
    for name, phase in summary["phases"].items():
        if not phase["closed"]:
            _warn("ram", "phase has no cgroup peak", name=name, **phase)
    write_json(args.output, summary)
    _info("ram", "wrote", out=str(args.output),
          phases={name: (block["rss_sampled"]["stats_mb"] or {}).get("mean_mb")
                  for name, block in summary["windows"].items()})


if __name__ == "__main__":
    main()