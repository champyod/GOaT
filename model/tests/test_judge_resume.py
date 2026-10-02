"""Crash + resume for the real-screenshot CPU judge.

The judge walks 500 screenshots per model, so every per-image record has to be
on disk before the next image is decoded. These tests drive the real worker
entry point (`real_cpu.run_job`) with a fake backend over real (tiny) PNGs: no
network, no GPU, no weights. `scripts/judge_real_cpu.py` is imported for the
CLI side (it installs the path-only `goat_model.ocr` package stub, so
`ocr/__init__` never drags in torch), and its `main()` never runs here.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import judge_real_cpu as cli  # the sys.path entry above has to come first

from goat_model.ocr import judge_report, real_cpu, record_log
from goat_model.ocr.engine import OCRResult
from goat_model.ocr.real_data import Asset, discover_assets

MODEL = "PP-OCRv5-mobile"
OTHER = "ThaiTrOCR"
KEYED = 4  # images carrying a key file; the rest stay inference-only


class Crash(BaseException):
    """Deliberately not an `Exception`: a bad screenshot is recorded as a
    record, so a session dying mid-pass has to get past both per-image guards."""


class FakeBackend:
    """Recognizes nothing real. The hypothesis comes off the pixel sum, so CER
    is deterministic and every image scores differently."""

    def __init__(self, crash_on: int | None = None) -> None:
        self.calls = 0
        self._crash_on = crash_on

    def recognize(self, image: np.ndarray) -> OCRResult:
        if self._crash_on is not None and self.calls >= self._crash_on:
            raise Crash(f"session died after {self.calls} images")
        self.calls += 1
        return OCRResult(text=f"text{int(image.sum()) % 97}", latency_ms=1.0)


def _dataset(root: Path, names: list[str]) -> list[Asset]:
    """A tiny flat dataset: real PNGs plus a key file for the first `KEYED`."""
    (root / "gt").mkdir(parents=True)
    for index, name in enumerate(names):
        cv2.imwrite(str(root / name), np.full((8, 12, 3), index + 1, dtype=np.uint8))
        if index < KEYED:
            (root / "gt" / f"{Path(name).stem}.txt").write_text(f"key {index}",
                                                                encoding="utf-8")
    return discover_assets(root)


def _job(assets: list[Asset], samples: Path, partial: Path, model: str = MODEL,
         repeats: int = 1) -> real_cpu.Job:
    return real_cpu.Job(
        model=model, assets=assets, img_size=None, seed=42, repeats=repeats,
        core=None, mem_cap_bytes=None, samples_path=samples, partial_path=partial)


def _args(tmp_path: Path, force: bool = False, repeats: int = 1) -> SimpleNamespace:
    return SimpleNamespace(output=tmp_path / "judge.json", seed=42, repeats=repeats,
                           force=force, models=MODEL, mem_cap_mb=None,
                           local_dir=tmp_path / "data")


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    """The two files a run writes, named as the CLI would name them."""
    return tmp_path / "judge.json.samples_test.jsonl", tmp_path / "judge.json.partial.json"


def _use(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> FakeBackend:
    _complete_torch_stub()
    monkeypatch.setattr(real_cpu, "build_backend", lambda model, seed: backend)
    return backend


def _complete_torch_stub() -> None:
    """Round out a torch test double left behind by the neighbouring OCR tests.

    A base install has no torch, so those tests put an empty module in
    sys.modules, and two things then break on it: `utils.setup_seed` calls
    `manual_seed`, and scipy's array-API layer reads `torch.Tensor` while
    importing `scipy.stats` for the paired t-test. Adding both attributes keeps
    the worker's real seeding and the real metrics in play instead of stubbing
    them out, and the judge report needs the comparison block to build at all.
    """
    torch = sys.modules.get("torch")
    if torch is None:
        return
    if not hasattr(torch, "manual_seed"):
        torch.manual_seed = lambda seed: None
    if not hasattr(torch, "Tensor"):
        torch.Tensor = type("Tensor", (), {})


def _rows(samples: Path) -> list[dict]:
    return record_log.read_samples(samples)


def _logged_stems(partial: Path, model: str) -> set[str]:
    return {stem for stems in record_log.partial_stems(partial, model).values()
            for stem in stems}


def _crash_run(job: real_cpu.Job, crash_on: int,
               monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = _use(monkeypatch, FakeBackend(crash_on=crash_on))
    with pytest.raises(Crash):
        real_cpu.run_job(job)
    return backend


def test_appending_then_resuming_records_each_image_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png", "d.png"])
    samples, partial = _paths(tmp_path)

    first = _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, samples, partial))
    assert first.calls == len(assets)

    # Second start over the same log: nothing re-scored, nothing re-appended.
    second = _use(monkeypatch, FakeBackend())
    result = real_cpu.run_job(_job(assets, samples, partial))

    assert second.calls == 0
    rows = _rows(samples)
    assert [row["image"] for row in rows] == ["a.png", "b.png", "c.png", "d.png"]
    assert all(row["model"] == MODEL and row["repeat_index"] == 0 for row in rows)
    assert result["runs"] == [rows]
    assert _logged_stems(partial, MODEL) == {"a.png", "b.png", "c.png", "d.png"}

    # And a third: the guard reads the log, so restarts cannot stack records.
    third = _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, samples, partial))

    assert third.calls == 0
    assert _rows(samples) == rows


def test_record_written_before_a_crash_survives_the_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png", "d.png", "e.png"])
    samples, partial = _paths(tmp_path)

    assert _crash_run(_job(assets, samples, partial), 2, monkeypatch).calls == 2
    survived = _rows(samples)
    assert [row["image"] for row in survived] == ["a.png", "b.png"]

    resumed = _use(monkeypatch, FakeBackend())
    result = real_cpu.run_job(_job(assets, samples, partial))

    assert resumed.calls == len(assets) - 2
    assert [row["image"] for row in _rows(samples)] == [
        "a.png", "b.png", "c.png", "d.png", "e.png"]
    # The resumed pass reports the pre-crash records, not fresh scores for them.
    assert result["runs"][0][:2] == survived
    assert result["runs"][0] == _rows(samples)


def test_resume_keeps_every_repeat_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png"])
    samples, partial = _paths(tmp_path)
    job = _job(assets, samples, partial, repeats=2)

    # 3 images of repeat 0 plus 1 of repeat 1, then the session dies.
    _crash_run(job, 4, monkeypatch)
    assert [(row["repeat_index"], row["image"]) for row in _rows(samples)] == [
        (0, "a.png"), (0, "b.png"), (0, "c.png"), (1, "a.png")]

    _use(monkeypatch, FakeBackend())
    result = real_cpu.run_job(job)

    assert [len(run) for run in result["runs"]] == [3, 3]
    assert _logged_stems(partial, MODEL) == {"a.png", "b.png", "c.png"}
    assert len(_rows(samples)) == 6


def test_partial_never_names_an_image_the_log_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png", "d.png"])
    samples, partial = _paths(tmp_path)
    real = record_log.checkpoint

    def die_before_the_second_checkpoint(path: Path, model: str, repeat_index: int,
                                         stems: Any) -> None:
        if len(stems) == 2:
            raise Crash("died between the append and the checkpoint")
        real(path, model, repeat_index, stems)

    # That gap is the only window where the two files can disagree: the record
    # has to be on disk and the checkpoint behind it, never ahead of it.
    monkeypatch.setattr(record_log, "checkpoint", die_before_the_second_checkpoint)
    with pytest.raises(Crash):
        real_cpu.run_job(_job(assets, samples, partial))
    assert len(_rows(samples)) == 2
    assert _logged_stems(partial, MODEL) == {"a.png"}

    monkeypatch.setattr(record_log, "checkpoint", real)
    _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, samples, partial))
    logged = {row["image"] for row in _rows(samples)}
    assert _logged_stems(partial, MODEL) <= logged
    assert logged == {asset.image.name for asset in assets}


def test_force_ignores_the_checkpoint_and_rescores_every_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png"])
    partial, first_samples = cli._record_paths(_args(tmp_path), [MODEL])
    _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, first_samples, partial))
    assert len(_rows(first_samples)) == len(assets)
    first_run_id = record_log.load_partial(partial, 42, 1)["run_id"]

    forced_partial, forced_samples = cli._record_paths(_args(tmp_path, force=True), [MODEL])
    forced_doc = record_log.load_partial(forced_partial, 42, 1)

    assert forced_samples != first_samples
    assert not forced_samples.exists()
    assert len(_rows(first_samples)) == len(assets)  # --force never truncates history
    assert forced_doc["run_id"] != first_run_id
    assert forced_doc["models"] == {}

    backend = _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, forced_samples, forced_partial))

    assert backend.calls == len(assets)
    assert len(_rows(forced_samples)) == len(assets)


def test_resume_reuses_the_run_id_and_logs_what_it_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png"])
    partial, samples = cli._record_paths(_args(tmp_path), [MODEL])
    _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, samples, partial))
    capsys.readouterr()

    assert cli._record_paths(_args(tmp_path), [MODEL]) == (partial, samples)

    logged = capsys.readouterr().out
    assert "resuming" in logged
    assert f"model={MODEL}" in logged
    assert f"skipped={len(assets)}" in logged


def test_report_from_the_log_equals_the_report_from_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png", "d.png", "e.png"])
    samples, partial = _paths(tmp_path)
    jobs = [_job(assets, samples, partial, model=model) for model in (MODEL, OTHER)]
    _use(monkeypatch, FakeBackend())
    outcomes = [real_cpu.run_job(job) for job in jobs]
    args = SimpleNamespace(seed=42, repeats=1)
    monkeypatch.setattr(judge_report, "_mem_available", lambda: 12345)

    on_disk = judge_report.build_report(jobs, outcomes, assets, args, samples)
    in_memory = judge_report.report_core(
        jobs, outcomes, assets, args,
        {job["model"]: job["runs"] for job in outcomes}, str(samples))

    assert judge_report.json_safe(on_disk) == judge_report.json_safe(in_memory)
    assert on_disk["models"][MODEL]["coverage"]["n_images"] == len(assets)
    assert on_disk["models"][OTHER]["coverage"]["n_missing_key"] == len(assets) - KEYED
    assert on_disk["sample_log"] == str(samples)
    assert on_disk["comparisons"][0]["n_images"] == KEYED


def test_cli_writes_the_report_from_the_log_and_clears_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png"])
    args = _args(tmp_path)
    monkeypatch.setattr(cli, "_parse_args", lambda: args)
    monkeypatch.setattr(cli, "_assets", lambda root: assets)
    monkeypatch.setattr(cli, "run_jobs", _inline_run_jobs)
    _complete_torch_stub()
    monkeypatch.setattr(real_cpu, "build_backend", lambda model, seed: FakeBackend())

    assert cli.main() == cli.EXIT_OK

    report = json.loads(args.output.read_text(encoding="utf-8"))
    assert set(report["models"]) == {MODEL}
    assert report["models"][MODEL]["coverage"]["n_images"] == len(assets)
    assert len(record_log.read_samples(Path(report["sample_log"]))) == len(assets)
    assert not record_log.partial_path(args.output).exists()


def _inline_run_jobs(jobs: list, outcomes: list[dict],
                     on_progress: Any) -> None:
    """`run_jobs` without `spawn`, so the CLI path is testable without weights."""
    for job in jobs:
        outcomes.append(real_cpu.run_job(job))
        on_progress(outcomes)


def test_job_survives_the_spawn_pickling(tmp_path: Path) -> None:
    assets = _dataset(tmp_path / "data", ["a.png"])
    job = _job(assets, tmp_path / "log.jsonl", tmp_path / "checkpoint.json")
    assert pickle.loads(pickle.dumps(job)) == job


def test_record_carries_every_field_the_report_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _dataset(tmp_path / "data", ["a.png", "b.png", "c.png", "d.png", "e.png"])
    samples, partial = _paths(tmp_path)
    _use(monkeypatch, FakeBackend())
    real_cpu.run_job(_job(assets, samples, partial))

    rows = _rows(samples)
    assert set(rows[0]) == {
        "model", "repeat_index", "image", "reference", "reference_nonempty", "hypothesis",
        "cer", "word_accuracy", "latency_ms", "scored", "error"}
    assert rows[0]["reference"] == "key 0"
    # Past KEYED the key file is gone: inference-only, still recorded, never zeroed.
    assert rows[KEYED]["reference"] is None and rows[KEYED]["cer"] is None
    assert json.loads(samples.read_text(encoding="utf-8").splitlines()[0]) == rows[0]


def test_duplicate_record_in_the_log_is_refused(tmp_path: Path) -> None:
    samples, _ = _paths(tmp_path)
    row = {"model": MODEL, "repeat_index": 0, "image": "a.png"}
    with record_log.SampleLog(samples) as log:
        log.append(row)
        log.append(dict(row))
    with pytest.raises(ValueError, match="duplicate record"):
        record_log.index_rows(_rows(samples))


def test_broken_record_line_fails_loud(tmp_path: Path) -> None:
    samples, _ = _paths(tmp_path)
    samples.write_text('{"model": "PP-OCRv5-mobile"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a record line"):
        record_log.read_samples(samples)


def test_unwritable_sample_log_fails_the_model(tmp_path: Path) -> None:
    assets = _dataset(tmp_path / "data", ["a.png"])
    samples = tmp_path / "a-directory.jsonl"
    samples.mkdir()
    partial = tmp_path / "judge.json.partial.json"

    result = real_cpu.run_job(_job(assets, samples, partial))

    assert result["status"] == "error"
    assert str(samples) in result["error"]
    assert not partial.exists()  # nothing was scored, so nothing was recorded


def test_checkpoint_rename_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / "judge.json.partial.json"
    record_log.start_partial(partial, "run-1", 42, 1)
    real_replace = record_log.os.replace

    def die_before_the_rename(source: str, target: str) -> None:
        raise Crash("died after the temp write, before the rename")

    monkeypatch.setattr(record_log.os, "replace", die_before_the_rename)
    with pytest.raises(Crash):
        record_log.checkpoint(partial, MODEL, 0, ["a.png"])

    monkeypatch.setattr(record_log.os, "replace", real_replace)
    assert json.loads(partial.read_text(encoding="utf-8")) == {
        "run_id": "run-1", "seed": 42, "repeats": 1, "models": {}}
    record_log.checkpoint(partial, MODEL, 0, ["a.png"])
    assert record_log.partial_stems(partial, MODEL) == {0: ["a.png"]}


def test_checkpoint_of_another_configuration_is_ignored(tmp_path: Path) -> None:
    partial = tmp_path / "judge.json.partial.json"
    record_log.start_partial(partial, "run-1", 42, 1)
    assert record_log.load_partial(partial, 42, 1)["run_id"] == "run-1"
    assert record_log.load_partial(partial, 7, 1) == {}
    assert record_log.load_partial(partial, 42, 3) == {}
