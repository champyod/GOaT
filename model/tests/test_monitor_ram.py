"""Two declared memory windows, three kernel sources, and nothing unlabelled.

Every kernel source is faked here — `/proc/<pid>/status`, `memory.peak`, and the
sampler's own clock — so the whole file runs in milliseconds with no target process,
no sleeps and no cgroup of its own. What it pins are the decisions a report depends
on: each window reads its own figures at its own close, a peak whose reset failed is
reported unavailable instead of published, an overlapping run is refused, the boundary
instant belongs to one window whichever order the windows were declared in, and a figure
that could not be measured names the reason instead of sitting null.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

_HERE = Path(__file__).resolve()
_MODEL = _HERE.parents[1]
for _entry in (str(_MODEL / "src"), str(_MODEL / "scripts")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import monitor_ram as ram  # the sys.path entries above have to come first

#: A live pid, so `main()`'s own `psutil.Process(pid)` construction is real while every
#: kernel source it then reads is stubbed.
PID = os.getpid()
MB = ram.BYTES_PER_MB
INTERVAL_S = 1.0
IDLE = ram.Window("idle", 0.0, 10.0)
ACTIVE = ram.Window("active", 20.0, 10.0)
WINDOWS = [IDLE, ACTIVE]

#: Two windows that touch — idle ends at 60 s and active opens on that same instant, so one
#: instant is both a close and an open.
TOUCHING_IDLE = ram.Window("idle", 0.0, 60.0)
TOUCHING_ACTIVE = ram.Window("active", 60.0, 60.0)


def _phases(windows: list[ram.Window] = WINDOWS) -> dict[str, dict]:
    """Phase records in the state `main()` builds them in."""
    return {window.name: {"opened": False, "closed": False, "cgroup": None,
                          "cgroup_reset": None, "status_at_close": None}
            for window in windows}


def _status(anon_mb: float, file_mb: float, hwm_mb: float) -> dict[str, int]:
    """`/proc/<pid>/status` counters in bytes, as `_proc_status` returns them."""
    anon, file_backed = int(anon_mb * MB), int(file_mb * MB)
    return {"VmRSS": anon + file_backed, "VmHWM": int(hwm_mb * MB), "RssAnon": anon,
            "RssFile": file_backed, "RssShmem": 0}


def _peak(peak_mb: float) -> dict:
    """A readable `memory.peak` in bytes, as `read_cgroup_peak` returns it."""
    return {"available": True, "reason": None, "cgroup_path": "/fake/cgroup",
            "peak_bytes": int(peak_mb * MB), "current_bytes": int(40 * MB),
            "resettable": True}


def _samples() -> list[dict]:
    """One polled sample per second: 45 MB through idle, 120 MB through active, so
    each window's `cgroup_peak >= rss_max >= anon + file` can hold at once."""
    return [{"t_s": float(second),
             "rss_bytes": int((45 if second < ACTIVE.start_s else 120) * MB),
             "vm_hwm_bytes": int((50 if second < ACTIVE.start_s else 130) * MB)}
            for second in range(int(ACTIVE.end_s) + 2)]


def _advance(times: list[float], phases: dict[str, dict]) -> None:
    """Run the sampler's own window state machine at these instants, in order."""
    for now_s in times:
        ram._advance_windows(PID, now_s, WINDOWS, phases)


def _boundary_counters() -> dict[str, float]:
    """A mutable `memory.peak` and `VmHWM` in MB, which `_install_boundary_sources` reads."""
    return {"peak_mb": 0.0, "hwm_mb": 300.0}


#: `memory.peak` and `VmHWM` as the independent counters they are, stepped over two windows
#: touching at 60 s: idle runs up to a 300 MB peak, active opens on that same instant and
#: runs up to 100 MB, and the kernel's own high-water mark grows only afterwards. `None`
#: leaves a counter where it is, because only the figure the current window owns is set
#: at an instant.
BOUNDARY_STEPS = ((0.0, 0.0, 300.0), (30.0, 300.0, 300.0), (60.0, None, None),
                  (90.0, 100.0, 400.0), (120.0, None, None))


def _install_boundary_sources(monkeypatch: pytest.MonkeyPatch,
                              counters: dict[str, float]) -> None:
    """Fake the reset, `memory.peak` and `/proc/<pid>/status` onto one mutable counter
    each, so a window reads the value the kernel figure held when it closed."""

    def fake_reset(pid: int) -> bool:
        counters["peak_mb"] = 0.0
        return True

    monkeypatch.setattr(ram, "reset_cgroup_peak", fake_reset)
    monkeypatch.setattr(ram, "read_cgroup_peak", lambda pid: _peak(counters["peak_mb"]))
    monkeypatch.setattr(ram, "_proc_status",
                        lambda pid: _status(90.0, 10.0, counters["hwm_mb"]))


def _drive_boundary(counters: dict[str, float], windows: list[ram.Window],
                    phases: dict[str, dict]) -> None:
    """Advance the fakes to each of `BOUNDARY_STEPS` and the sampler's state machine with
    them, in whatever order `windows` was declared."""
    for now_s, peak_mb, hwm_mb in BOUNDARY_STEPS:
        if peak_mb is not None:
            counters["peak_mb"] = peak_mb
        if hwm_mb is not None:
            counters["hwm_mb"] = hwm_mb
        ram._advance_windows(PID, now_s, windows, phases)


def _block(window: ram.Window, phases: dict[str, dict]) -> dict:
    """The report block for one window, built the way `_build_summary` builds it."""
    return ram._window_block(_samples(), window, INTERVAL_S, phases[window.name])


def _stub_run(monkeypatch: pytest.MonkeyPatch, output: Path, argv: list[str]) -> None:
    """Point `main()` at a stubbed clock and a stubbed target, so a refused or a
    completed run is decided without sampling anything."""
    monkeypatch.setattr(sys, "argv", ["monitor_ram.py", *argv, "--output", str(output)])
    monkeypatch.setattr(ram, "_collect_samples",
                        lambda pid, args, windows, phases, proc: [])
    monkeypatch.setattr(ram, "_proc_status", lambda pid: _status(30.0, 10.0, 55.0))


def test_each_window_reads_status_at_its_own_close(monkeypatch: pytest.MonkeyPatch) -> None:
    phases = _phases()
    idle_status = _status(30.0, 10.0, 55.0)
    active_status = _status(90.0, 10.0, 130.0)
    reads: list[dict[str, int]] = []

    def fake_status(pid: int) -> dict[str, int]:
        # the target allocates its second block only after the idle window has closed
        read = idle_status if not reads else active_status
        reads.append(read)
        return read

    monkeypatch.setattr(ram, "_proc_status", fake_status)
    monkeypatch.setattr(ram, "reset_cgroup_peak", lambda pid: True)
    monkeypatch.setattr(ram, "read_cgroup_peak", lambda pid: _peak(200.0))

    _advance([0.0, 5.0, 9.5], phases)
    assert [phases[name]["cgroup_reset"] for name in ("idle", "active")] == [True, None]
    assert reads == []  # idle has not closed yet, so nothing has been read from it

    _advance([10.0], phases)  # idle's declared end
    assert reads == [idle_status]
    assert phases["active"]["status_at_close"] is None  # active is untouched so far

    _advance([20.0, 30.0], phases)
    assert reads == [idle_status, active_status]
    assert [phases[name]["status_at_close"] for name in ("idle", "active")] == [idle_status,
                                                                                active_status]

    idle, active = _block(IDLE, phases), _block(ACTIVE, phases)
    assert idle["anon_vs_file"]["anon_bytes"] == 30 * MB
    assert active["anon_vs_file"]["anon_bytes"] == 90 * MB
    assert idle["vm_hwm"]["value_at_window_end_mb"] == 55.0
    assert active["vm_hwm"]["value_at_window_end_mb"] == 130.0
    # one read per close is what makes the two windows differ; a single read at the end
    # of the run would hand both of them the process's last state
    assert len(reads) == len(WINDOWS)
    for block in (idle, active):
        assert block["vm_hwm"]["value_at_window_end_reason"] is None


def test_peak_from_a_failed_reset_is_reported_unavailable_not_published(
    monkeypatch: pytest.MonkeyPatch) -> None:
    phases = _phases()
    monkeypatch.setattr(ram, "_proc_status", lambda pid: _status(30.0, 10.0, 55.0))
    monkeypatch.setattr(ram, "reset_cgroup_peak", lambda pid: False)
    monkeypatch.setattr(ram, "read_cgroup_peak", lambda pid: _peak(300.0))

    _advance([0.0, 10.5, 20.0, 30.5], phases)

    read_at_s = {"idle": 10.5, "active": 30.5}
    for window in WINDOWS:
        block = _block(window, phases)["cgroup_peak"]
        assert block["available"] is False
        assert block["cgroup_reset"] is False
        assert block["peak_bytes"] is None and block["peak_mb"] is None
        assert block["current_bytes"] is None
        assert "could not be reset" in block["reason"]
        # the stale 300 MB is dropped, and the path it came from is kept for provenance
        assert block["cgroup_path"] == "/fake/cgroup"
        assert block["read_at_s"] == read_at_s[window.name]


def test_an_unreadable_close_read_names_why_both_figures_are_null(
    monkeypatch: pytest.MonkeyPatch) -> None:
    phases = _phases()
    monkeypatch.setattr(ram, "_proc_status", lambda pid: {})  # the process died, or /proc denied it
    monkeypatch.setattr(ram, "reset_cgroup_peak", lambda pid: True)
    monkeypatch.setattr(ram, "read_cgroup_peak", lambda pid: _peak(200.0))

    _advance([0.0, 10.5], phases)

    block = _block(IDLE, phases)
    anon = block["anon_vs_file"]
    assert anon["available"] is False
    assert anon["reason"] == ram.CLOSE_STATUS_UNREADABLE_REASON
    assert [anon[key] for key in ("anon_bytes", "file_bytes", "shmem_bytes", "rss_bytes",
                                  "parts_sum_bytes", "residual_bytes")] == [None] * 6
    assert block["vm_hwm"]["value_at_window_end_mb"] is None
    assert block["vm_hwm"]["value_at_window_end_reason"] == ram.CLOSE_STATUS_UNREADABLE_REASON
    # the polled figure is a different source and survives the failed read
    assert block["rss_sampled"]["stats_mb"]["mean_mb"] == pytest.approx(45.0)
    assert block["cgroup_peak"]["available"] is True


def test_a_window_the_run_never_reaches_names_its_reason(
    monkeypatch: pytest.MonkeyPatch) -> None:
    phases = _phases()
    monkeypatch.setattr(ram, "_proc_status", lambda pid: _status(30.0, 10.0, 55.0))
    monkeypatch.setattr(ram, "reset_cgroup_peak", lambda pid: True)
    monkeypatch.setattr(ram, "read_cgroup_peak", lambda pid: _peak(200.0))

    _advance([0.0, 10.5], phases)  # the run ends inside active's start

    block = _block(ACTIVE, phases)
    assert block["cgroup_peak"]["available"] is False
    assert "never opened" in block["cgroup_peak"]["reason"]
    assert block["cgroup_peak"]["peak_mb"] is None
    assert block["anon_vs_file"]["reason"] == ram.NEVER_CLOSED_REASON
    assert block["vm_hwm"]["value_at_window_end_reason"] == ram.NEVER_CLOSED_REASON


def test_overlapping_windows_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "ram.json"
    _stub_run(monkeypatch, output, ["--pid", str(PID), "--idle-start", "0",
                                    "--idle-duration", "60", "--active-start", "30",
                                    "--active-duration", "60"])

    with pytest.raises(ValueError, match=r"'idle' and 'active' overlap"):
        ram.main()

    assert not output.exists()  # a refused run writes no measurement


def test_touching_windows_are_the_declared_boundary_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "ram.json"
    _stub_run(monkeypatch, output, ["--pid", str(PID), "--idle-start", "0",
                                    "--idle-duration", "60", "--active-start", "60",
                                    "--active-duration", "60"])

    counters = _boundary_counters()

    def drive(pid: int, args: argparse.Namespace, windows: list[ram.Window],
              phases: dict[str, dict], proc: psutil.Process) -> list[dict]:
        """Stand in for the poll loop over the two windows `main()` declared, so the
        boundary is the only thing the assertions rest on."""
        _drive_boundary(counters, windows, phases)
        return []

    _install_boundary_sources(monkeypatch, counters)
    monkeypatch.setattr(ram, "_collect_samples", drive)

    ram.main()

    summary = json.loads(output.read_text(encoding="utf-8"))
    idle, active = summary["windows"]["idle"], summary["windows"]["active"]
    assert [(idle["window"]["start_s"], idle["window"]["end_s"]),
            (active["window"]["start_s"], active["window"]["end_s"])] == [(0.0, 60.0),
                                                                        (60.0, 120.0)]
    assert ram._first_overlap([TOUCHING_IDLE, TOUCHING_ACTIVE]) is None

    # the boundary is shared, so the reset that opens active must not land before idle has
    # read its own peak: 300 MB is idle's figure and 100 MB is active's
    assert idle["cgroup_peak"]["peak_mb"] == 300.0
    assert active["cgroup_peak"]["peak_mb"] == 100.0
    assert [idle["cgroup_peak"]["read_at_s"],
            active["cgroup_peak"]["read_at_s"]] == [60.0, 120.0]
    # the same ordering bounds the kernel read: idle's VmHWM is taken before active grows
    assert idle["vm_hwm"]["value_at_window_end_mb"] == 300.0
    assert active["vm_hwm"]["value_at_window_end_mb"] == 400.0


def test_closes_precede_opens_with_the_windows_declared_active_first(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The same touching boundary, declared in the opposite order.

    Closing before opening is what keeps each window's own figures, and it has to hold
    whichever list the windows arrive in: a single pass in declaration order would let
    active's reset destroy idle's unread peak.
    """
    windows = [TOUCHING_ACTIVE, TOUCHING_IDLE]
    phases = _phases(windows)
    counters = _boundary_counters()
    _install_boundary_sources(monkeypatch, counters)

    _drive_boundary(counters, windows, phases)

    idle = ram._window_block([], TOUCHING_IDLE, INTERVAL_S, phases["idle"])
    active = ram._window_block([], TOUCHING_ACTIVE, INTERVAL_S, phases["active"])
    assert idle["cgroup_peak"]["peak_mb"] == 300.0
    assert active["cgroup_peak"]["peak_mb"] == 100.0
    assert idle["vm_hwm"]["value_at_window_end_mb"] == 300.0
    assert active["vm_hwm"]["value_at_window_end_mb"] == 400.0
    assert [idle["cgroup_peak"]["read_at_s"],
            active["cgroup_peak"]["read_at_s"]] == [60.0, 120.0]


def test_the_touching_boundary_sample_belongs_to_the_window_that_opens_there() -> None:
    phases = _phases([TOUCHING_IDLE, TOUCHING_ACTIVE])
    samples = [{"t_s": 30.0, "rss_bytes": int(45 * MB), "vm_hwm_bytes": None},
               {"t_s": 60.0, "rss_bytes": int(120 * MB), "vm_hwm_bytes": None}]

    idle = ram._window_block(samples, TOUCHING_IDLE, INTERVAL_S, phases["idle"])
    active = ram._window_block(samples, TOUCHING_ACTIVE, INTERVAL_S, phases["active"])

    # 60 s is idle's end and active's start, and a sample counted in both phases would
    # make both means describe neither window
    assert idle["rss_sampled"]["stats_mb"]["n"] == 1
    assert idle["rss_sampled"]["stats_mb"]["mean_mb"] == 45.0
    assert active["rss_sampled"]["stats_mb"]["n"] == 1
    assert active["rss_sampled"]["stats_mb"]["mean_mb"] == 120.0
    assert TOUCHING_IDLE.contains(60.0) is False
    assert TOUCHING_ACTIVE.contains(60.0) is True