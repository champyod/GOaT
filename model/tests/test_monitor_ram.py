"""Two declared memory windows, three kernel sources, and nothing unlabelled.

Every kernel source is faked here — `/proc/<pid>/status`, `memory.peak`, and the
sampler's own clock — so the whole file runs in milliseconds with no target process,
no sleeps and no cgroup of its own. What it pins are the decisions a report depends
on: each window reads its own figures at its own close, a peak whose reset failed is
reported unavailable instead of published, an overlapping run is refused, and a figure
that could not be measured names the reason instead of sitting null.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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


def _phases() -> dict[str, dict]:
    """Phase records in the state `main()` builds them in."""
    return {window.name: {"opened": False, "closed": False, "cgroup": None,
                          "cgroup_reset": None, "status_at_close": None}
            for window in WINDOWS}


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

    _advance([0.0, 5.0, 10.0], phases)
    assert [phases[name]["cgroup_reset"] for name in ("idle", "active")] == [True, None]
    assert reads == []  # idle has not closed yet, so nothing has been read from it

    _advance([10.5], phases)  # the first sample past idle's end
    assert reads == [idle_status]
    assert phases["active"]["status_at_close"] is None  # active is untouched so far

    _advance([20.0, 30.5], phases)
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

    ram.main()

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert [(summary["windows"][name]["window"]["start_s"],
             summary["windows"][name]["window"]["end_s"])
            for name in ("idle", "active")] == [(0.0, 60.0), (60.0, 120.0)]
    assert ram._first_overlap(WINDOWS) is None