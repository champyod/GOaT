"""Report half of the real-screenshot CPU judge (scripts/judge_real_cpu.py).

Turns raw per-image worker records into the judge document: per-model headline
metrics (CER / word-accuracy / latency), paired CER tests over the images both
models scored against a non-empty key, the fairness block, and up to MAX_SAMPLES
hypotheses.

The records are read back from the run's sample log
(`<output>.samples_<runid>.jsonl`, written image by image by each worker), not
from what a worker happened to still be holding in memory, so a run resumed
after a crash reports exactly what a run that never crashed would. `sample_log`
names that file: it holds every record, while `samples` is only the short
excerpt below.

Report invariants: the headline counts non-empty keys only (coverage.empty_key
accounts for the present-but-empty ones, coverage.n_comparable for the keyless
ones), a single repeat carries no dispersion, every non-finite float is nulled on
write because JSON has no NaN/Infinity literal, and the decision is never null -
it is deferred, because no script reads this report: the author applies the
OCR_CER_THRESHOLD rule to it by hand.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import numpy as np

from goat_model import constants as c
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.metrics import bootstrap_ci, cohens_d, paired_t_test, summarize
from goat_model.ocr import record_log
from goat_model.ocr.real_cpu import DEVICE, LATENCY_SCOPE, TAG

METRICS = ("cer", "word_accuracy", "latency_ms")
MAX_SAMPLES = 10
DEFER_INFERENCE_ONLY = "inference-only: n_comparable == 0, so no image pair is paired-testable"
DEFER_TBD = (
    "else TBD: pairs compared above; the report author applies the "
    f"OCR_CER_THRESHOLD={c.OCR_CER_THRESHOLD} rule by hand - no script reads this report"
)


def _in_headline(record: dict, key: str) -> bool:
    """Headline CER/accuracy counts non-empty keys only; latency is key-free.

    A present-but-empty key scores 0.0 whenever the hypothesis is empty, so
    averaging it in would flatter a silent model: coverage.empty_key reports it.
    """
    return key == "latency_ms" or record["reference_nonempty"]


def _metric_block(runs: list[list[dict]], key: str, seed: int) -> dict:
    columns = ([r[key] for r in run if r[key] is not None and _in_headline(r, key)]
               for run in runs)
    per_run = [float(np.mean(values)) for values in columns if values]
    mean = float(np.mean(per_run)) if per_run else None
    if len(per_run) < 2:  # a single pass has no dispersion; nan must not reach the JSON
        return {"mean": mean, "n_runs": len(per_run), "std": None, "ci95": None}
    _, std = summarize(per_run)
    return {
        "mean": mean, "n_runs": len(per_run), "std": std, "ci95": bootstrap_ci(per_run, seed=seed),
    }


def _coverage(runs: list[list[dict]]) -> dict:
    """Every record accounted for, plus the empty keys the headline excludes."""
    records = [rec for run in runs for rec in run]
    empty = [rec for rec in records if rec["scored"] and not rec["reference_nonempty"]]
    empty_cer = [rec["cer"] for rec in empty if rec["cer"] is not None]
    return {
        "n_images": len(runs[0]) if runs else 0,
        "n_with_key_file": sum(1 for r in records if r["scored"]),
        "n_missing_key": sum(1 for r in records if not r["scored"]),
        "n_comparable": sum(1 for r in records if r["reference_nonempty"]),
        "n_failed_images": sum(1 for r in records if r["error"]),
        "empty_key": {
            "n": len(empty),
            "n_nonempty_hypothesis": sum(1 for r in empty if r["hypothesis"].strip()),
            "mean_cer": None if not empty_cer else float(np.mean(empty_cer)),
        },
    }


def aggregate(runs: list[list[dict]], seed: int) -> dict:
    return {
        "status": "ok", "n_repeats": len(runs), "coverage": _coverage(runs),
        "metrics": {key: _metric_block(runs, key, seed) for key in METRICS}}


def _per_image_cer(runs: list[list[dict]]) -> dict[str, float]:
    """Mean CER per image across repeats, non-empty keys only."""
    buckets: dict[str, list[float]] = {}
    for run in runs:
        for rec in run:
            if rec["cer"] is None or not rec["reference_nonempty"]:
                continue
            buckets.setdefault(rec["image"], []).append(float(rec["cer"]))
    return {name: float(np.mean(values)) for name, values in buckets.items()}


def compare(runs_by_model: dict[str, list[list[dict]]]) -> list[dict]:
    """Paired t-test + Cohen's d per model pair over their shared, non-empty keys."""
    per_image = {model: _per_image_cer(runs) for model, runs in runs_by_model.items()}
    out: list[dict] = []
    for a, b in combinations(sorted(per_image), 2):
        names = sorted(set(per_image[a]) & set(per_image[b]))
        entry: dict = {
            "a": a, "b": b, "metric": "cer", "n_images": len(names),
            "paired_t_test": None, "cohens_d": None}
        if len(names) < 2:
            entry["skipped"] = "fewer than 2 images carry a non-empty key in both models"
        else:
            first = [per_image[a][n] for n in names]
            second = [per_image[b][n] for n in names]
            entry["paired_t_test"] = paired_t_test(first, second, alpha=c.OCR_ALPHA)
            entry["cohens_d"] = cohens_d(first, second)
            if first == second:  # zero within-pair variance: t/p are NaN, nulled on write
                entry["note"] = "identical per-image CER: t and p are undefined (null)"
        out.append(entry)
    return out


def samples(runs_by_model: dict[str, list[list[dict]]], limit: int = MAX_SAMPLES) -> list[dict]:
    """Up to `limit` non-empty hypotheses, round-robin over models for coverage."""
    pools = {m: [r for run in runs for r in run if r["hypothesis"].strip()][:limit]
             for m, runs in runs_by_model.items()}
    picked = [
        (model, pools[model][i]) for i in range(limit) for model in sorted(pools)
        if i < len(pools[model])]
    return [
        {"model": m, "image": rec["image"], "hypothesis": rec["hypothesis"]} for m, rec in picked]


def _mem_available() -> int | None:
    """Host MemAvailable now, next to the cap, so an OOM is attributable."""
    import psutil

    return int(psutil.virtual_memory().available)


def _fairness(jobs: list, outcomes: list[dict], assets: list, args) -> dict:
    seen = {o["model"]: o for o in outcomes}

    def reported(model: str) -> dict:
        """What this model's worker captured, empty while it is still running."""
        return seen.get(model, {})

    cores = {j.model: {"assigned_core": j.core, "pinned": reported(j.model).get("pinned")}
             for j in jobs}
    return {
        "cores": cores,
        "mem_cap": {
            "requested_bytes": jobs[0].mem_cap_bytes if jobs else None,
            "applied_bytes": {j.model: reported(j.model).get("mem_cap") for j in jobs},
            "reported_by_worker": {j.model: reported(j.model).get("reported") for j in jobs},
            "note": "no RLIMIT_AS unless --mem-cap-mb is given: torch/paddle need the headroom "
                    "to load, so a fixed cap would OOM them rather than contain them",
            "mem_available_bytes": _mem_available()},
        "device": DEVICE, "seed": args.seed, "repeats": args.repeats,
        "image_order": {
            "n_images": len(assets), "first": assets[0].image.name, "last": assets[-1].image.name,
            "sorted_by": "image file name - one shared list, identical order for every model",
            "img_size": {j.model: j.img_size for j in jobs}},
        "latency_scope": LATENCY_SCOPE,
    }


def json_safe(value: object) -> object:
    """Null every non-finite float: JSON has no NaN/Infinity literal.

    Ints and bools pass through unchanged, so counts stay counts in the report.
    """
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    return value


def _model_block(worker: dict, runs: list[list[dict]], seed: int) -> dict:
    """One model's headline block, from the records its worker left on disk.

    A worker that reported ok but recorded nothing is an error rather than an
    empty success: an empty success would read as a model that failed every
    image without the failure being attributable.
    """
    if worker["status"] != "ok":
        return {"status": "error", "error": worker["error"]}
    if not runs:
        return {"status": "error", "error": "worker reported ok but recorded no image"}
    return aggregate(runs, seed)


def report_core(jobs: list, outcomes: list[dict], assets: list, args,
                by_model: dict[str, list[list[dict]]], sample_log: str | None) -> dict:
    """The judge document from grouped records; partial while models are outstanding."""
    models = {o["model"]: _model_block(o, by_model.get(o["model"], []), args.seed)
              for o in outcomes}
    done = {o["model"]: by_model[o["model"]] for o in outcomes
            if o["status"] == "ok" and by_model.get(o["model"])}
    comparable = min((m.get("coverage", {}).get("n_comparable", 0) for m in models.values()),
                     default=0)
    return {
        "models": models,
        "comparisons": compare(done),
        "decision": {
            "status": "deferred",
            "reason": DEFER_TBD if comparable else DEFER_INFERENCE_ONLY,
        },
        "fairness": _fairness(jobs, outcomes, assets, args),
        "sample_log": sample_log,
        "samples": samples(done),
    }


def build_report(jobs: list, outcomes: list[dict], assets: list, args,
                 samples_path: Path | None) -> dict:
    """`report_core` over the run's sample log - the record both a fresh and a
    resumed run produced, rather than whatever a worker still held in memory."""
    rows = record_log.read_samples(samples_path)
    by_model = record_log.runs_from_rows(rows, [asset.image.name for asset in assets])
    return report_core(jobs, outcomes, assets, args, by_model,
                       None if samples_path is None else str(samples_path))


def log_summary(outcomes: list[dict]) -> None:
    for o in outcomes:
        if o["status"] != "ok":
            _err(TAG, "model failed", model=o["model"], error=o["error"])
            continue
        records = [rec for run in o["runs"] for rec in run]
        scored = [r["cer"] for r in records if r["cer"] is not None and r["reference_nonempty"]]
        latencies = [r["latency_ms"] for r in records if r["latency_ms"] is not None]
        _info(TAG, "model", model=o["model"], images=len(records),
              failed=sum(1 for r in records if r["error"]),
              mean_cer=None if not scored else round(float(np.mean(scored)), 4),
              mean_latency_ms=None if not latencies else round(float(np.mean(latencies)), 1))
