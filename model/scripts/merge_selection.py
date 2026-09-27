#!/usr/bin/env python3
"""Merge parallel selection workers into one verdict.

Workers run with `--models`/`--datasets` subsets and distinct `--output`
files; this recomputes the decision from their per-model summaries and
writes the canonical `*_selection.json` that training reads.

Exactness: the OCR verdict (lowest pooled mean CER) is recovered exactly by
weighting dataset means with `n_images` (equal repeats enforced); the MT
verdict is exact because its rule already compares means. Pairwise
statistics need raw per-repeat series, which live only in full runs, so a
merged file carries an empty `comparisons` with a note pointing there.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.utils import log_call, write_json


def _read_inputs(inputs: list[Path]) -> list[dict]:
    docs = []
    for p in inputs:
        try:
            docs.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            raise SystemExit(f"merge: cannot read {p}: {err}")
    return docs


def _check_consistent(docs: list[dict], files: list[Path]) -> None:
    seeds = {d.get("seed") for d in docs}
    runs = {d.get("runs") for d in docs}
    if len(seeds) != 1 or len(runs) != 1:
        raise SystemExit(
            f"merge: workers disagree (seeds={sorted(seeds, key=str)}, runs={sorted(runs, key=str)}): "
            f"{[str(f) for f in files]}"
        )


def _merge_ocr(docs: list[dict]) -> tuple[dict, dict]:
    models: dict = {}
    for d in docs:
        for model, stats in d.get("models", {}).items():
            if model in models:
                raise SystemExit(f"merge: model {model!r} produced by two workers")
            models[model] = stats
    mean_cer: dict[str, float] = {}
    for model, stats in models.items():
        total_n, total_runs, weighted = 0, set(), 0.0
        for ds, s in stats.items():
            if ds in ("model", "seed"):
                continue
            total_runs.add(s["n_runs"])
            total_n += s["n_images"]
            weighted += s["n_images"] * s["cer"]["mean"]
        if len(total_runs) != 1:
            raise SystemExit(f"merge: model {model!r} mixes repeat counts: {sorted(total_runs)}")
        mean_cer[model] = weighted / total_n if total_n else float("nan")
    decision = min(mean_cer, key=lambda m: mean_cer[m])
    return models, {
        "rule": f"lowest mean CER over {', '.join(sorted(models))} (merged workers)",
        "mean_cer": mean_cer,
        "selected": decision,
    }


def _merge_mt(docs: list[dict]) -> tuple[dict, dict]:
    models: dict = {}
    for d in docs:
        for model, stats in d.get("models", {}).items():
            if model in models:
                raise SystemExit(f"merge: model {model!r} produced by two workers")
            models[model] = stats
    m0 = models["NLLB-200-distilled-600M"]["bleu"]["mean"]
    lat0 = models["NLLB-200-distilled-600M"]["avg_s_per_sentence"]["mean"]
    selected = (
        "NLLB-200-distilled-600M"
        if m0 > c.MT_BLEU_THRESHOLD and lat0 <= c.MT_LATENCY_THRESHOLD_S
        else "NLLB-200-distilled-1.3B"
    )
    return models, {
        "rule": f"NLLB-600M iff BLEU > {c.MT_BLEU_THRESHOLD} and latency <= {c.MT_LATENCY_THRESHOLD_S}s (merged workers)",
        "600m_bleu": m0,
        "600m_avg_s": lat0,
        "selected": selected,
    }


def merge_files(side: str, inputs: list[Path], output: Path) -> dict:
    """Merge worker result files; returns the merged document."""
    docs = _read_inputs(inputs)
    _check_consistent(docs, inputs)
    if side == "ocr":
        models, decision = _merge_ocr(docs)
    elif side == "mt":
        models, decision = _merge_mt(docs)
    else:
        raise SystemExit(f"merge: --side must be ocr|mt, got {side!r}")
    merged = {
        "runs": docs[0]["runs"],
        "seed": docs[0]["seed"],
        "merged_from": [str(p) for p in inputs],
        "models": models,
        "comparisons": [],
        "decision": decision,
    }
    merged["decision"]["note"] = "pairwise stats live in full-run files, not merges"
    write_json(output, merged)
    return merged


@log_call
def main() -> None:
    parser = argparse.ArgumentParser(description="Merge parallel selection worker outputs.")
    parser.add_argument("--side", choices=("ocr", "mt"), required=True)
    parser.add_argument("--inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    merged = merge_files(args.side, args.inputs, args.output)
    print(json.dumps(merged["decision"], ensure_ascii=False))


if __name__ == "__main__":
    main()
