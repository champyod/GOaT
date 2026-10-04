"""CPU judge worker side over real desktop screenshots (HF CCYod/Real-GOaT-OCR).

Library side of scripts/judge_real_cpu.py: one pinned worker process per model,
the per-worker core / address-space policy, and per-image scoring. Dataset
discovery and the layout guard live in goat_model/ocr/real_data.py; the
streaming write path (sample log + resume checkpoint) in
goat_model/ocr/record_log.py; the aggregation and report in
goat_model/ocr/judge_report.py.

Fairness invariants: every model walks the same image list, sorted by file name;
one record per image, scored with metrics.cer only (char-level, bounded max-len
normalization) - never WER/jiwer, which go negative on unspaced Thai; a missing
key file yields an inference-only record (cer=None) and a present-but-empty one
is scored yet reported apart from the headline metrics. Neither is scored zero.

Durability: a record is on disk before the next image is decoded, so a worker
that dies mid-pass leaves a pass worth resuming rather than a void.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from multiprocessing.queues import Queue
from pathlib import Path

import cv2
import numpy as np

try:
    import resource
except ImportError:  # non-unix: no RLIMIT_AS, the cap degrades to one log line
    resource = None

from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.metrics import cer
from goat_model.ocr import record_log
from goat_model.ocr.engine import (
    HybridLineOCR,
    OCRBackend,
    PaddleLineDetector,
    ThaiTrOCR,
    get_ocr,
)
from goat_model.ocr.evaluate import preprocess
from goat_model.ocr.real_data import Asset
from goat_model.utils import LogProgress, read_gt, setup_seed

TAG = "judge-real-cpu"
DEVICE = "cpu"  # the judge measures CPU cost; never route a model to CUDA
WORKER_THREADS = "1"  # OMP/MKL/etc: one thread, so a worker owns its core
PROGRESS_INTERVAL_S = 15.0
LATENCY_SCOPE = (
    "wall clock inside backend.recognize() for one image at a time in one pinned process. "
    "PP-OCRv5-mobile and ThaiTrOCR start the timer after _load(), so their latency excludes "
    "model load (engine.py:61-62,116-117); PPDet-ThaiTrOCR is built with one detector and "
    "one recognizer injected into it and reused for every image and repeat, so its first "
    "image pays both loads and every later image pays detection plus each crop it "
    "recognizes. Crop counts are not recorded (OCRResult carries text+latency)"
)


@dataclass(frozen=True)
class Job:
    """A worker's whole input: picklable, so it survives `spawn`."""

    model: str
    assets: list[Asset]
    img_size: int | None
    seed: int
    repeats: int
    core: int | None
    mem_cap_bytes: int | None  # None = no RLIMIT_AS (torch/paddle need the headroom)
    samples_path: Path  # the raw record this worker appends to, image by image
    partial_path: Path  # the shared resume checkpoint, extended after each image


def pin_core(core: int | None) -> bool:
    """Restrict this process and its children to one core; best-effort."""
    if core is None or not hasattr(os, "sched_setaffinity"):
        _warn(TAG, "core pinning unsupported here", core=core)
        return False
    try:
        os.sched_setaffinity(0, {core})
    except OSError as err:
        _warn(TAG, "core pinning refused", core=core, error=str(err))
        return False
    return True


def cap_address_space(cap_bytes: int | None) -> int | None:
    """Opt-in soft RLIMIT_AS cap; returns the applied limit, else None.

    None (no --mem-cap-mb) means no cap at all: torch and paddle reserve far more
    than 1 GiB while loading, so a fixed cap here OOMs the neural backends
    instead of containing them.
    """
    if cap_bytes is None:
        _info(TAG, "address-space cap not applied", reason="no --mem-cap-mb given")
        return None
    if resource is None:
        _warn(TAG, "address-space cap unsupported here")
        return None
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = cap_bytes if hard == resource.RLIM_INFINITY else min(cap_bytes, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (OSError, ValueError) as err:
        _warn(TAG, "address-space cap refused", error=str(err))
        return None
    return limit


def score_record(
    image: str, reference: str | None, hypothesis: str, latency_ms: float | None,
    error: str | None = None,
) -> dict:
    """One scored (or inference-only) record for one image.

    `reference is None` = key file absent: hypothesis and latency are kept, CER
    stays None. A present-but-empty key is scored but flagged
    `reference_nonempty=False`, which keeps it out of the headline metrics and
    the paired tests. `error` marks an image the worker could not decode or
    recognize: it carries no metric and does not end the pass.
    """
    scored = reference is not None
    value = cer(reference, hypothesis) if scored else None
    return {
        "image": image,
        "reference": reference,
        "reference_nonempty": bool(scored and reference.strip()),
        "hypothesis": hypothesis,
        "cer": value,
        # metrics.word_accuracy is 1 - CER (pinned by tests/test_metrics.py);
        # deriving it avoids a second Levenshtein sweep per image.
        "word_accuracy": None if value is None else 1.0 - value,
        "latency_ms": latency_ms,
        "scored": scored,
        "error": error,
    }


def _read_rgb(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode image: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _recognize_one(backend: OCRBackend, asset: Asset, img_size: int | None) -> dict:
    image = _read_rgb(asset.image)
    if img_size is not None:
        # None skips the resize: Tesseract segments full pages itself and
        # downscaling would cost it small text.
        image = preprocess(image, img_size)
    reference = read_gt(asset.gt) if asset.gt is not None else None
    result = backend.recognize(image)
    return score_record(asset.image.name, reference, result.text, result.latency_ms)


def _one_row(backend: OCRBackend, job: Job, asset: Asset, repeat_index: int,
             recorder: record_log.Recorder) -> dict:
    """One image, as one raw-log row: the stored row when a pass already scored
    it, else a fresh score appended to disk before the next image is decoded."""
    recorded = recorder.take(repeat_index, asset.image.name)
    if recorded is not None:
        return recorded
    try:
        record = _recognize_one(backend, asset, job.img_size)
    except Exception as err:  # one bad screenshot must not void the pass
        reason = f"{type(err).__name__}: {err}"
        _err(TAG, "image failed", model=job.model, image=asset.image.name, error=reason)
        record = score_record(asset.image.name, None, "", None, reason)
    row = {"model": job.model, "repeat_index": repeat_index, **record}
    recorder.commit(row)
    return row


def _recognize_pass(backend: OCRBackend, job: Job, repeat_index: int,
                    recorder: record_log.Recorder) -> list[dict]:
    setup_seed(job.seed)
    prog = LogProgress(
        len(job.assets), TAG, unit="img", interval_s=PROGRESS_INTERVAL_S,
        in_path=str(job.assets[0].image.parent) if job.assets else "?", out_path=job.model,
    )
    rows: list[dict] = []
    for asset in job.assets:
        rows.append(_one_row(backend, job, asset, repeat_index, recorder))
        prog.update()
    prog.close()
    return rows


def outcome(
    model: str, status: str, err: str | None, pinned: bool | None,
    cap: int | None, runs: list[list[dict]], reported: bool = True,
) -> dict:
    """Uniform worker result: a failed model is reported, never silently dropped.

    `pinned`/`mem_cap` are None when the worker never captured them;
    `reported=False` marks a result the parent built because its worker died.
    """
    return {"model": model, "status": status, "error": err, "pinned": pinned,
            "mem_cap": cap, "reported": reported, "runs": runs}


def _all_failed(runs: list[list[dict]]) -> str | None:
    """Why the pass is a model failure rather than a per-image hiccup.

    The per-image guard keeps one bad screenshot local, but a model that errored
    on every image never ran (missing extra, dead binary) and must not be
    reported as a slow model.
    """
    records = [rec for run in runs for rec in run]
    if records and all(rec["error"] for rec in records):
        return f"all {len(records)} images failed: {records[0]['error']}"
    return None


def build_backend(model: str, seed: int) -> OCRBackend:
    """One backend per worker, with the hybrid's model pair injected once.

    engine.HybridLineOCR.recognize() builds a fresh detector and recognizer
    whenever neither is injected (engine.py:213-214), so an un-injected hybrid
    pays both model loads again on every image. One injected pair per worker
    loads once; the per-image cost stays detection plus its crops.
    """
    if model != HybridLineOCR.name:
        return get_ocr(model, device=DEVICE, seed=seed)
    return HybridLineOCR(
        device=DEVICE, seed=seed,
        detector=PaddleLineDetector(device=DEVICE, seed=seed),
        recognizer=ThaiTrOCR(device=DEVICE, seed=seed),
    )


def run_job(job: Job) -> dict:
    """One model in one pinned process: `--repeats` sequential passes."""
    pinned = pin_core(job.core)
    cap = cap_address_space(job.mem_cap_bytes)
    try:
        # The log opens before the weights do: a path that cannot be written has
        # to fail the model now, not discard a scored image at the end of a pass.
        with record_log.SampleLog(job.samples_path) as log:
            recorder = record_log.Recorder(
                job.model, job.repeats, job.samples_path, job.partial_path, log)
            _info(TAG, "worker start", model=job.model, core=job.core, pinned=pinned,
                  mem_cap=cap, images=len(job.assets), resumed=recorder.resumed)
            backend = build_backend(job.model, job.seed)
            runs = [_recognize_pass(backend, job, index, recorder)
                    for index in range(job.repeats)]
    except Exception as err:
        _err(TAG, "worker failed", model=job.model, error=str(err))
        return outcome(job.model, "error", str(err), pinned, cap, [])
    reason = _all_failed(runs)
    if reason is not None:
        _err(TAG, "worker failed on every image", model=job.model, error=reason)
        return outcome(job.model, "error", reason, pinned, cap, runs)
    return outcome(job.model, "ok", None, pinned, cap, runs)


def worker_main(job: Job, sink: Queue) -> None:
    """`spawn` entry point: the parent always gets a result, even on a crash."""
    try:
        sink.put(run_job(job))
    except Exception as err:
        _err(TAG, "worker crashed", model=job.model, error=str(err))
        sink.put(outcome(job.model, "error", f"crashed: {err}", False, None, []))
