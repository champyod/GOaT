#!/usr/bin/env python3
"""Cold start of a GOaT app: the kernel's boot anchor to a named seam inside the app.

A cold start has two ends and they sit on two different clocks. The start is the machine
coming up, which is an event the kernel records whether or not anybody was watching; the
end is a seam inside the app, which only the app can stamp. This harness reads the start
from the kernel and the end from stamps the app writes, and puts both on one timeline:
CLOCK_BOOTTIME, which counts from boot and keeps counting across a suspend. Three segments
come out of that, and their sum is what a person waiting for the window waits through:

* `boot_to_app_start_s` - boot to the app process starting, read from field 22 of
  `/proc/<pid>/stat` rather than from this script's own start, so the figure does not depend
  on when the operator launched the harness.
* `app_start_to_ready_s` - the app process starting to the app's event loop reporting Ready,
  which is the `RunEvent::Ready` the run callback receives.
* `ready_to_page_loaded_s` - Ready to the main webview's page load finishing
  (`PageLoadEvent::Finished`), which is the endpoint, and not the overlay the proposal names.

`app_start_to_models_ready_s` is recorded as a fourth point and is deliberately outside the
sum: the page asks for the models and does not wait for the answer, and the deck's own figure
excludes model-loading time (`sections/GOAT-final-slides/06-analysis.typ:136`). It is recorded
only when the stamps carry a field for it; a build that stamps no such seam leaves it null with
the reason beside it rather than approximating it.

The report's stated method stops at "the point at which the memory used has stopped
changing" (`sections/GOAT-report/analysis.typ:183`), which is not an instant anything can
read: it is a number settling under a threshold no document states, judged by eye. The
endpoint here is a named seam instead, so this interval is not that quantity and a figure
from this harness must not be quoted as one.

Over reboots the harness reports the median and the interquartile range and computes
neither a mean nor a standard deviation. Every per-reboot value is kept, so any other
statistic stays derivable from the record instead of from this script's arithmetic.

A seam the app never reached is null with the reason beside it. Nothing is filled in.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, write_json

PROC_ROOT = Path("/proc")
PROC_STAT = PROC_ROOT / "stat"
PROC_UPTIME = PROC_ROOT / "uptime"
PROC_MEMINFO = PROC_ROOT / "meminfo"

SCHEMA = "goat-coldstart/1"
AGGREGATE_SCHEMA = "goat-coldstart-aggregate/1"
DEFAULT_OUTPUT = Path("results/coldstart.json")
DEFAULT_AGGREGATE_OUTPUT = Path("results/coldstart_aggregate.json")
AWAIT_STAMPS_S = 30.0
POLL_INTERVAL_S = 0.25

#: Two differences taken from two separately read clocks do not come out equal, so a
#: difference smaller than this is read as rounding rather than as a suspended machine. It is
#: three orders of magnitude above the observed drift between two adjacent reads and far
#: below any suspension short enough to be a scheduling accident.
SUSPEND_NOISE_TOLERANCE_S = 1e-3

#: What can be said about the page cache in front of a launch. It is declared by the
#: operator and never inferred from the sample below, which is evidence and not a verdict:
#: `Cached` counts every page the kernel is holding, not the model files in particular.
PAGE_CACHE_STATES = ("cold after reboot", "warm - models already resident", "unknown")

MEMINFO_KEYS = ("Cached", "MemAvailable", "MemTotal", "SReclaimable")

STAMPS_METHOD = (
    "the JSON the app writes at its own seams; a boottime field is taken as it stands and a "
    "wall-clock field is reduced to the boot timeline by subtracting btime, which holds only "
    "while the wall clock has not been stepped since boot, so a reduced stamp is marked "
    "derived and its source field is named"
)

if not hasattr(time, "CLOCK_BOOTTIME"):
    raise RuntimeError(
        "CLOCK_BOOTTIME is not available here. This harness measures cold start against the "
        "kernel's own boot timeline and will not substitute another clock for it."
    )
CLOCK_BOOTTIME = time.CLOCK_BOOTTIME
CLOCK_TICK_HZ = os.sysconf("SC_CLK_TCK")


class Seam(NamedTuple):
    """One point on the boot timeline, named the way the app names it.

    `boottime_field` and `wall_field` are the two forms the same stamp may take. The app
    writes one of them; the other is not read, and a run that carries neither records the
    seam as absent rather than approximating it.
    """

    name: str
    boottime_field: str
    wall_field: str


SEAMS = (
    Seam("app_start", "app_start_boottime_s", "app_start_wall_unix_ms"),
    Seam("ready", "ready_boottime_s", "ready_wall_unix_ms"),
    Seam("page_loaded", "page_loaded_boottime_s", "page_loaded_wall_unix_ms"),
    Seam("models_ready", "models_ready_boottime_s", "models_ready_wall_unix_ms"),
)

#: segment name, then the seams it runs between. The last one is outside the sum on purpose.
SEGMENTS = (
    ("boot_to_app_start_s", ("app_start",)),
    ("app_start_to_ready_s", ("app_start", "ready")),
    ("ready_to_page_loaded_s", ("ready", "page_loaded")),
    ("app_start_to_models_ready_s", ("app_start", "models_ready")),
)

SUM_SEGMENTS = ("boot_to_app_start_s", "app_start_to_ready_s", "ready_to_page_loaded_s")
SUM_ENDPOINT = "boot_to_page_loaded_s"

NO_STAMPS_REASON = (
    "the app-side stamps are not instrumented yet, so no seam after boot carries a value; "
    "see artefact.method.app_stamps.status"
)

STEADINESS_NOTE = (
    "The report's endpoint is 'the point at which the memory used has stopped changing' "
    "(sections/GOAT-report/analysis.typ:183), which names no seam, no sampling interval and "
    "no threshold, and the 8 s figure printed against it is one reading rather than five. The "
    "endpoint here is ready_to_page_loaded_s, a seam the app stamps. The two intervals start "
    "at the same place and do not end at the same place, so this figure is not that figure."
)


def _boottime_s() -> float:
    """Seconds on the boot timeline: from boot, counting through a suspend."""
    return time.clock_gettime(CLOCK_BOOTTIME)


def _monotonic_s() -> float:
    """Seconds from an arbitrary origin, which stops while the machine is suspended."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _suspend_total_s() -> float:
    """Seconds this machine has spent suspended since boot.

    CLOCK_BOOTTIME keeps running while suspended and CLOCK_MONOTONIC stops, so the gap
    between them is the suspended total and not a measurement of anything this app did.
    """
    return _boottime_s() - _monotonic_s()


def _btime_unix_s() -> tuple[float | None, str]:
    """The kernel's `btime`: the boot instant on the Unix timeline, or why there is none.

    `btime` is the only wall-clock statement the kernel keeps about when it came up, and it
    is recorded once at boot, so a wall clock stepped afterwards leaves it describing a
    boot instant the epoch timeline no longer agrees with.
    """
    try:
        for line in PROC_STAT.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1]), ""
    except (OSError, ValueError, IndexError) as exc:
        return None, f"/proc/stat is unreadable: {exc}"
    return None, "/proc/stat carries no btime field on this kernel"


def _uptime_s() -> tuple[float | None, str]:
    """`/proc/uptime`'s first field: seconds since boot, suspend included."""
    try:
        return float(PROC_UPTIME.read_text(encoding="utf-8").split()[0]), ""
    except (OSError, ValueError, IndexError) as exc:
        return None, f"/proc/uptime is unreadable: {exc}"


def _meminfo_block() -> dict:
    """The page-cache counters read before the launch, in bytes, with the reason if absent.

    `Cached` counts every page the kernel is holding, not the app's model files, so this
    block is evidence beside `page_cache_state` and never a substitute for it.
    """
    values: dict[str, int | None] = {key: None for key in MEMINFO_KEYS}
    try:
        text = PROC_MEMINFO.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {**values, "reason": f"/proc/meminfo is unreadable: {exc}"}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if separator and key in values:
            try:
                values[key] = int(rest.split()[0]) * 1024
            except (ValueError, IndexError) as exc:
                values[key] = None
                _warn("coldstart", "meminfo counter unparsed", key=key, error=str(exc))
    return {**values, "reason": None}


def _proc_start_boottime_s(pid: int) -> tuple[float | None, dict]:
    """Field 22 of `/proc/<pid>/stat` as seconds since boot, and the raw field beside it.

    The field is a count of clock ticks since boot, so dividing by SC_CLK_TCK puts it on
    CLOCK_BOOTTIME directly. The command name in field 2 may itself contain spaces and
    parentheses, so the fields after the final `)` are counted from there rather than by
    splitting the whole line.
    """
    stat_path = PROC_ROOT / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, {"measured": False, "pid": pid, "raw_starttime_ticks": None,
                      "proc_start_boottime_s": None, "reason": f"{stat_path} unreadable: {exc}"}
    fields = text.rpartition(")")[2].split()
    index = 22 - 3
    if len(fields) <= index:
        return None, {"measured": False, "pid": pid, "raw_starttime_ticks": None,
                      "proc_start_boottime_s": None,
                      "reason": f"{stat_path} has {len(fields)} fields after the command name, "
                                "so field 22 is not there to read"}
    try:
        ticks = int(fields[index])
    except ValueError as exc:
        return None, {"measured": False, "pid": pid, "raw_starttime_ticks": None,
                      "proc_start_boottime_s": None,
                      "reason": f"field 22 of {stat_path} is not an integer: {exc}"}
    return ticks / CLOCK_TICK_HZ, {"measured": True, "pid": pid, "raw_starttime_ticks": ticks,
                                   "proc_start_boottime_s": ticks / CLOCK_TICK_HZ, "reason": None}


def _host_block() -> dict:
    """The host facts a cold-start figure is only valid for."""
    uname = platform.uname()
    return {"os": uname.system, "kernel": uname.release, "cpu_machine": uname.machine,
            "cpu_cores_total": os.cpu_count(), "memory_total_bytes": _meminfo_block()["MemTotal"],
            "clock_tick_hz": CLOCK_TICK_HZ, "python_version": platform.python_version()}


def _boot_anchor() -> dict:
    """The boot instant on every timeline a segment could be read against.

    Two residuals are recorded so a later reader can tell which timeline drifted:
    `captured_boottime_s` against `/proc/uptime` (both count since boot, so they should
    agree), and the wall clock against `btime` (they agree only while the wall clock has not
    been stepped since boot).
    """
    boottime_s, monotonic_s, wall_unix_s = _boottime_s(), _monotonic_s(), time.time()
    boot_unix_s, boot_reason = _btime_unix_s()
    uptime_s, uptime_reason = _uptime_s()
    return {
        "boot_boottime_s": 0.0,
        "boot_unix_s": boot_unix_s,
        "boot_unix_reason": boot_reason,
        "captured_boottime_s": boottime_s,
        "captured_monotonic_s": monotonic_s,
        "captured_wall_unix_s": wall_unix_s,
        "uptime_s": uptime_s,
        "uptime_reason": uptime_reason,
        "boottime_minus_uptime_s": None if uptime_s is None else boottime_s - uptime_s,
        "wall_minus_boot_unix_s": None if boot_unix_s is None else wall_unix_s - boot_unix_s,
        "clock_tick_hz": CLOCK_TICK_HZ,
        "method": "btime from /proc/stat, uptime from /proc/uptime, and the three clocks read "
                  "with clock_gettime at this instant",
    }


def _await_file(path: Path, timeout_s: float, interval_s: float) -> bool:
    """Whether `path` appeared within `timeout_s`, polled every `interval_s`."""
    deadline_s = _boottime_s() + timeout_s
    while _boottime_s() < deadline_s:
        if path.exists():
            return True
        time.sleep(interval_s)
    return path.exists()


def _read_stamps(path: Path) -> tuple[dict | None, str]:
    """The app's stamps, or the reason there are none.

    A file that exists but does not parse is reported as unreadable rather than treated as
    an empty row: a half-written line from a launch that is still in progress reads exactly
    like one from a crash, and the two must not produce the same figure.
    """
    if not path.exists():
        return None, f"{path} does not exist"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} could not be read as JSON: {exc}"
    if not isinstance(raw, dict):
        return None, f"{path} is a JSON {type(raw).__name__}, not an object of stamps"
    return raw, ""


def _stamp_instant(stamps: dict, seam: Seam, boot_unix_s: float | None) -> tuple[float | None, dict]:
    """One seam's instant on the boot timeline, and how that instant was obtained.

    A wall-clock stamp is reduced by subtracting `btime`, which is arithmetic on two numbers
    rather than a measurement of the seam, so the block says which form was read and whether
    it was derived. A wall-clock stamp with no `btime` to reduce it by stays null: the
    difference is not a smaller figure, it is no figure.
    """
    boottime_raw = stamps.get(seam.boottime_field)
    if isinstance(boottime_raw, (int, float)):
        return float(boottime_raw), {"seam": seam.name, "clock": "CLOCK_BOOTTIME",
                                     "source_field": seam.boottime_field,
                                     "source_value": boottime_raw, "derived": False,
                                     "reason": None}
    wall_raw = stamps.get(seam.wall_field)
    if not isinstance(wall_raw, (int, float)):
        return None, {"seam": seam.name, "clock": None, "source_field": None,
                      "source_value": None, "derived": False,
                      "reason": f"the stamps carry no {seam.boottime_field} and no "
                                f"{seam.wall_field}"}
    if boot_unix_s is None:
        return None, {"seam": seam.name, "clock": "unix_wall_ms", "source_field": seam.wall_field,
                      "source_value": wall_raw, "derived": False,
                      "reason": f"{seam.wall_field} is a wall-clock stamp and this host's "
                                "btime could not be read, so it cannot be placed on the boot "
                                "timeline"}
    return wall_raw / 1000.0 - boot_unix_s, {
        "seam": seam.name, "clock": "unix_wall_ms", "source_field": seam.wall_field,
        "source_value": wall_raw, "derived": True,
        "reason": None,
    }


def _seam_blocks(stamps: dict | None, boot_unix_s: float | None) -> dict[str, dict]:
    """Every seam with its instant and how it was read; absent seams carry the reason.

    `app_start` is additionally cross-checked against the kernel's own field 22 for the same
    process, because two clocks of the same event should agree and a disagreement is a fact
    about the run rather than something to average away.
    """
    blocks: dict[str, dict] = {}
    for seam in SEAMS:
        if stamps is None:
            blocks[seam.name] = {"seam": seam.name, "boottime_s": None, "clock": None,
                                 "source_field": None, "derived": False, "reason": NO_STAMPS_REASON}
            continue
        instant, block = _stamp_instant(stamps, seam, boot_unix_s)
        blocks[seam.name] = {"seam": seam.name, "boottime_s": instant, **block}
    return blocks


def _cross_check_app_start(seams: dict[str, dict], pid: int | None) -> dict:
    """The app's own app_start stamp against field 22 of `/proc/<pid>/stat`.

    Both are the same event on the same timeline, so they should agree to within the clock
    tick the kernel counts them in. A larger gap is recorded here and left standing.
    """
    if pid is None:
        return {"measured": False, "reason": "no --pid was passed, so the kernel's own record "
                                            "of the app process start was not read"}
    proc_s, block = _proc_start_boottime_s(pid)
    block["method"] = (f"/proc/{pid}/stat field 22 divided by SC_CLK_TCK "
                       f"({CLOCK_TICK_HZ} Hz), a tick count since boot")
    app_s = seams["app_start"]["boottime_s"] if "app_start" in seams else None
    if proc_s is None or app_s is None:
        block["delta_ms"] = None
        block["agrees"] = None
        block["cross_check_reason"] = ("one of the two figures is absent, so there is nothing "
                                       "to compare")
        return block
    block["delta_ms"] = (app_s - proc_s) * 1000.0
    block["agrees"] = abs(block["delta_ms"]) <= 1000.0 / CLOCK_TICK_HZ
    block["cross_check_reason"] = None
    return block


def _difference(earlier: float | None, later: float | None) -> float | None:
    """`later - earlier`, or None when either end is absent.

    The order is never swapped to make a figure positive: a segment that came out negative
    is a real reading and stays as it is.
    """
    if earlier is None or later is None:
        return None
    return later - earlier


def _segment_blocks(seams: dict[str, dict]) -> dict[str, dict]:
    """Each segment's seconds, or the seam whose absence is why there is no figure."""
    blocks: dict[str, dict] = {}
    for name, endpoints in SEGMENTS:
        values = [seams[seam]["boottime_s"] for seam in endpoints]
        seconds = _difference(values[0], values[-1]) if len(values) > 1 else values[0]
        missing = [seam for seam, value in zip(endpoints, values) if value is None]
        blocks[name] = {
            "segment": name,
            "seconds": seconds,
            "from_seam": endpoints[0],
            "to_seam": endpoints[-1],
            "in_sum": name in SUM_SEGMENTS,
            "reason": None if not missing else
                      f"{', '.join(missing)} carries no instant: " +
                      seams[missing[0]]["reason"],
        }
    return blocks


def _sum_block(segments: dict[str, dict], seams: dict[str, dict]) -> dict:
    """The endpoint twice: straight from boot to the seam, and as the three segments added.

    The two are computed independently so a segment that does not add up to its own endpoint
    shows as a residual rather than as a total nobody checked.
    """
    parts = [segments[name]["seconds"] for name in SUM_SEGMENTS]
    measured = [value for value in parts if value is not None]
    total = _difference(0.0, seams["page_loaded"]["boottime_s"])
    return {
        "segment": SUM_ENDPOINT,
        "seconds": total,
        "sum_of_segments_s": None if len(measured) < len(SUM_SEGMENTS) else sum(measured),
        "residual_s": None if (total is None or len(measured) < len(SUM_SEGMENTS))
                      else total - sum(measured),
        "segments_added": list(SUM_SEGMENTS),
        "excluded": {"app_start_to_models_ready_s": "the OCR engine is loaded after the page is "
                                                  "up, and sections/GOAT-final-slides/"
                                                  "06-analysis.typ:136 states the cold-start "
                                                  "figure excludes model-loading time"},
        "in_sum": True,
        "from_seam": "boot",
        "to_seam": "page_loaded",
        "reason": None if total is not None else seams["page_loaded"]["reason"],
    }


def _clock_block(seams: dict[str, dict], pid: int | None) -> dict:
    """Which clock each figure was read on, and the rule for subtracting them.

    Every figure is put on CLOCK_BOOTTIME before any segment is taken, so a segment is
    always a difference of two readings of one clock. The two clocks that are not the boot
    clock - the kernel's tick count and the app's wall clock - are named where they enter,
    and the wall clock's reduction is the one operation here that is arithmetic rather than
    a measurement.
    """
    return {
        "boot_anchor": "CLOCK_BOOTTIME via clock_gettime; btime and /proc/uptime as cross-checks",
        "app_start_kernel": f"/proc/<pid>/stat field 22 / {CLOCK_TICK_HZ} Hz, ticks since boot",
        "app_start_app": seams["app_start"]["clock"],
        "ready": seams["ready"]["clock"],
        "page_loaded": seams["page_loaded"]["clock"],
        "models_ready": seams["models_ready"]["clock"],
        "cross_clock_rule": "no two readings of different clocks are ever subtracted: an app "
                             "stamp on the wall clock is reduced to the boot timeline by "
                             "btime first, and the reduction is marked derived on the seam it "
                             "produced",
        "suspend_rule": "CLOCK_BOOTTIME counts time the machine spent suspended and "
                        "CLOCK_MONOTONIC does not, so a suspend inside the measured interval "
                        "is inside these segments; that is what a user waited through",
        "kernel_cross_checked": pid is not None,
    }


def _suspend_block(anchor_suspend_s: float, after_suspend_s: float) -> dict:
    """Whether the machine was suspended between this harness starting and the stamps.

    The two readings bracket the whole interval only because the harness was started before
    the app was launched, which is the order the boot anchor has to be read in.

    The two differences are each the result of a subtraction of two separately read clocks,
    so they differ from one another by rounding even on a machine that never suspended. The
    raw difference is reported next to the flag and the flag only fires above
    `SUSPEND_NOISE_TOLERANCE_S`, so a figure of a fraction of a millisecond cannot be read
    as a machine that went to sleep.
    """
    difference_s = after_suspend_s - anchor_suspend_s
    return {
        "suspended_total_s_at_launch": anchor_suspend_s,
        "suspended_total_s_after_stamps": after_suspend_s,
        "suspended_inside_interval_s": difference_s,
        "suspend_inside_interval": abs(difference_s) > SUSPEND_NOISE_TOLERANCE_S,
        "noise_tolerance_s": SUSPEND_NOISE_TOLERANCE_S,
        "method": "clock_gettime(CLOCK_BOOTTIME) minus clock_gettime(CLOCK_MONOTONIC) at two "
                  "instants; the gap is the suspended time, which the boot clock includes and "
                  "the monotonic clock omits. Two separate reads of two clocks do not return "
                  "the same difference twice, so a difference below noise_tolerance_s is "
                  "treated as no suspension rather than as one",
    }


def _capture_run(args: argparse.Namespace) -> dict:
    """One reboot's row: the anchor, the seams, the segments, and the run's own conditions.

    The anchor is read before the launch, so the harness has to be running before the app
    is started; a run whose anchor was read afterwards would still be able to place the app
    process's own start, but it could say nothing about a suspend in between.
    """
    anchor = _boot_anchor()
    suspend_at_launch = _suspend_total_s()
    page_cache = _meminfo_block()
    appeared = _await_file(args.stamps, args.await_stamps_s, args.poll_interval_s)
    stamps, stamps_reason = _read_stamps(args.stamps)
    suspend_after = _suspend_total_s()
    if not appeared:
        _warn("coldstart", "no stamps file appeared within the wait", path=str(args.stamps),
              await_s=args.await_stamps_s)
    if stamps is None and stamps_reason:
        _warn("coldstart", "no app stamps to read", reason=stamps_reason)
    seams = _seam_blocks(stamps, anchor["boot_unix_s"])
    segments = _segment_blocks(seams)
    return {
        "schema": SCHEMA,
        "produced_by": "GOaT/model/scripts/measure_coldstart.py",
        "captured_at_unix_s": anchor["captured_wall_unix_s"],
        "reboot_index": args.reboot_index,
        "page_cache_state": args.page_cache_state,
        "page_cache_state_options": list(PAGE_CACHE_STATES),
        "page_cache_before_launch": page_cache,
        "boot_anchor": anchor,
        "app_stamps": {"path": str(args.stamps), "row_kind": (stamps or {}).get("row_kind"),
                       "present": stamps is not None, "reason": stamps_reason,
                       "method": STAMPS_METHOD},
        "seams": seams,
        "kernel_cross_check": _cross_check_app_start(seams, args.pid),
        "segments": segments,
        "total": _sum_block(segments, seams),
        "suspend": _suspend_block(suspend_at_launch, suspend_after),
        "clock_used": _clock_block(seams, args.pid),
        "steadiness": {"report_endpoint": "the point at which the memory used has stopped "
                                          "changing, which names no seam and no threshold",
                       "measured_endpoint": "ready_to_page_loaded_s",
                       "same_interval": False, "note": STEADINESS_NOTE},
        "host": _host_block(),
        "complete": seams["page_loaded"]["boottime_s"] is not None,
    }


def _median_and_iqr(values: list[float]) -> dict:
    """Median, quartiles and IQR of `values`, with the quartile method named.

    The quartiles use the inclusive method - linear interpolation between the order
    statistics - and stay null below two observations, where the IQR is undefined rather
    than zero. No mean and no standard deviation is computed here.
    """
    if not values:
        return {"n": 0, "values_s": [], "median_s": None, "q1_s": None, "q3_s": None,
                "iqr_s": None, "min_s": None, "max_s": None,
                "reason": "no run carried a value for this segment"}
    ordered = sorted(values)
    block = {"n": len(ordered), "values_s": ordered, "median_s": statistics.median(ordered),
             "min_s": ordered[0], "max_s": ordered[-1]}
    if len(ordered) < 2:
        return {**block, "q1_s": None, "q3_s": None, "iqr_s": None,
                "reason": "one observation carries a median but no interquartile range"}
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return {**block, "q1_s": quartiles[0], "q3_s": quartiles[2],
            "iqr_s": quartiles[2] - quartiles[0], "reason": None}


def _aggregate(records: list[dict]) -> dict:
    """Median and IQR per segment across reboots, from each record's own figures.

    A record that never reached a seam contributes no value to that segment and is named
    under `records_without`, so a figure computed over four reboots is never read as a
    figure over five.
    """
    per_segment: dict[str, dict] = {}
    for name, _ in SEGMENTS + ((SUM_ENDPOINT, ()),):
        values: list[float] = []
        without: list[dict] = []
        for record in records:
            block = record["total"] if name == SUM_ENDPOINT else record["segments"].get(name)
            seconds = (block or {}).get("seconds")
            if seconds is None:
                without.append({"path": record.get("source_path"), "reboot_index":
                                record.get("reboot_index"),
                                "reason": (block or {}).get("reason", "segment absent")})
            else:
                values.append(seconds)
        per_segment[name] = {**_median_and_iqr(values), "records_without": without}
    return {
        "schema": AGGREGATE_SCHEMA,
        "produced_by": "GOaT/model/scripts/measure_coldstart.py",
        "n_records": len(records),
        "statistics_rule": "median and interquartile range over reboots; no mean and no "
                           "standard deviation is computed, because over a handful of reboots "
                           "a mean is the one run that was busy. Every per-reboot value is "
                           "kept under values_s, so any other statistic stays derivable from "
                           "the record",
        "quartile_method": "statistics.quantiles(n=4, method='inclusive'), linear "
                           "interpolation between order statistics; null below two "
                           "observations",
        "reboots": [{"path": record.get("source_path"),
                     "reboot_index": record.get("reboot_index"),
                     "page_cache_state": record.get("page_cache_state"),
                     "complete": record.get("complete"),
                     "suspend_inside_interval": (record.get("suspend") or {}).get(
                         "suspend_inside_interval")} for record in records],
        "per_segment": per_segment,
    }


def _load_records(paths: list[Path]) -> list[dict]:
    """The per-reboot run files, each tagged with where it came from.

    A file that does not parse stops the aggregation rather than being skipped: an aggregate
    over the runs that happened to be readable is not an aggregate over the reboots.
    """
    records: list[dict] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{path} is not readable as a run record: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            raise RuntimeError(f"{path} is not a {SCHEMA} record, so it carries no segment "
                               "figures this aggregation can use")
        records.append({**raw, "source_path": str(path)})
    return records


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cold start: boot anchor to a named app seam.")
    parser.add_argument("--stamps", type=Path,
                        help="the JSON the app writes at its own seams; without it no seam "
                             "after boot carries a value")
    parser.add_argument("--pid", type=int,
                        help="the app process, so field 22 of its /proc entry cross-checks the "
                             "app's own app_start stamp")
    parser.add_argument("--page-cache-state", choices=PAGE_CACHE_STATES,
                        help="what the page cache held in front of the launch; never inferred")
    parser.add_argument("--await-stamps-s", type=float, default=AWAIT_STAMPS_S,
                        help="how long to wait for the app to write its stamps")
    parser.add_argument("--poll-interval-s", type=float, default=POLL_INTERVAL_S)
    parser.add_argument("--reboot-index", type=int,
                        help="which reboot this is, 1-based; the operator's own count")
    parser.add_argument("--records", type=Path, nargs="+",
                        help="per-reboot run files to aggregate into a median and an IQR; "
                             "selects the aggregate mode and takes no capture arguments")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    """Reject an argument set that cannot produce a figure, before any clock is read."""
    if args.records:
        if args.output is None:
            args.output = DEFAULT_AGGREGATE_OUTPUT
        return
    if args.stamps is None:
        raise ValueError("--stamps is required: the endpoint of a cold start is a seam the app "
                         "stamps, and this harness will not invent one")
    if args.page_cache_state is None:
        raise ValueError("--page-cache-state is required: a cold start over a warm page cache "
                         "is a different figure, and a sample of Cached is not a verdict on "
                         "which model files are resident")
    if args.output is None:
        args.output = DEFAULT_OUTPUT
    if args.await_stamps_s < 0 or args.poll_interval_s <= 0:
        raise ValueError("--await-stamps-s must not be negative and --poll-interval-s must be "
                         "positive")


@log_call
def main() -> None:
    args = _parse_args()
    _validate_args(args)
    if args.records:
        summary = _aggregate(_load_records(args.records))
        write_json(args.output, summary)
        _info("coldstart", "wrote aggregate", out=str(args.output),
              n_records=summary["n_records"],
              median_total_s=summary["per_segment"][SUM_ENDPOINT]["median_s"])
        return
    run = _capture_run(args)
    write_json(args.output, run)
    _info("coldstart", "wrote", out=str(args.output), page_cache_state=run["page_cache_state"],
          boot_to_page_loaded_s=run["total"]["seconds"], complete=run["complete"])


if __name__ == "__main__":
    main()