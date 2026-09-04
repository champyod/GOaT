#!/usr/bin/env python3
"""OCR model selection: PP-OCRv5-mobile vs ThaiTrOCR.

Runs both models on both public datasets for `--repeats` runs, reports
mean±std + 95% CI, then applies the decision rule (hypothesis 2 / methodology):
pick ThaiTrOCR iff its CER <= 0.10, otherwise pick the lowest CER model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import dataset_revisions
from goat_model.metrics import cohens_d, paired_t_test
from goat_model.ocr import evaluate
from goat_model.ocr.engine import get_ocr
from goat_model.utils import LogProgress, setup_seed, write_json


def _partial_path(output: Path) -> Path:
    return output.with_name(output.stem + ".partial.json")


def _model_file(output: Path, model: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_")
    return output.with_name(f"{output.stem}.{safe}.json")


def _load_partial(path: Path, seed: int, repeats: int) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("seed") != seed or data.get("runs") != repeats:
        return {}
    return data


def _recs(triples: list) -> list[dict]:
    return [{"cer": c, "word_accuracy": w, "latency_ms": m} for c, w, m in triples]


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR selection experiment (CER decision rule).")
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    parser.add_argument("--ocr-eval-dir", type=Path, default=c.OCR_EVAL)
    parser.add_argument("--device", default="auto", help="auto=cuda if available else cpu")
    parser.add_argument("--force", action="store_true", help="ignore checkpoints, rerun all repeats")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()
    if not args.force and args.output.is_file():
        print(f"skipped - already selected: {args.output} (use --force to rerun)", flush=True)
        return

    setup_seed(args.seed)
    import torch
    device = args.device if args.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[select-ocr] device={device}", flush=True)
    results: dict = {
        "runs": args.repeats,
        "seed": args.seed,
        "dataset_revisions": dataset_revisions(),
        "models": {},
        "comparisons": [],
    }

    cer_by_model: dict[str, list[float]] = {}
    partial_path = _partial_path(args.output)
    saved = {} if args.force else _load_partial(partial_path, args.seed, args.repeats)
    saved_runs = saved.get("runs_data", {})
    if saved:
        print(f"[select-ocr] resuming from {partial_path}", flush=True)
    for model in c.OCR_MODELS:
        stats = {}
        for dataset in c.OCR_DATASETS:
            dataset_dir = args.ocr_eval_dir / dataset
            assets = evaluate.discover_assets(dataset_dir)
            backend = get_ocr(model, device=device, seed=args.seed)
            img_size = c.OCR_IMG_SIZE[model]
            print(f"[select-ocr] {model}/{dataset} — loading weights (first run downloads GBs)", flush=True)
            key = f"{model}/{dataset}"
            stored = [list(r) for r in saved_runs.get(key, [])]
            done = len(stored)
            prog = LogProgress(args.repeats, f"select-ocr {model}/{dataset}", unit="repeat", interval_s=30.0)
            prog.n = done
            runs = [_recs(r) for r in stored]
            triples = [list(r) for r in stored]
            for _ in range(done, args.repeats):
                recs = evaluate.run_ocr(backend, assets, img_size, seed=args.seed)
                runs.append(recs)
                triples.append([[rec["cer"], rec["word_accuracy"], rec["latency_ms"]] for rec in recs])
                saved_runs[key] = triples
                write_json(partial_path, {"seed": args.seed, "runs": args.repeats, "runs_data": saved_runs})
                prog.update()
            prog.close()
            summary = evaluate.aggregate_records(runs)
            stats[dataset] = summary
            cer_by_model.setdefault(model, []).extend(rec["cer"] for run in runs for rec in run)
        results["models"][model] = stats
        write_json(_model_file(args.output, model), {"model": model, "seed": args.seed, **stats})

    thai_mean = sum(cer_by_model["ThaiTrOCR"]) / len(cer_by_model["ThaiTrOCR"])
    pp_mean = sum(cer_by_model["PP-OCRv5-mobile"]) / len(cer_by_model["PP-OCRv5-mobile"])
    test = paired_t_test(
        cer_by_model["ThaiTrOCR"], cer_by_model["PP-OCRv5-mobile"], alpha=c.OCR_ALPHA
    )
    results["comparisons"].append(
        {
            "a": "ThaiTrOCR",
            "b": "PP-OCRv5-mobile",
            "paired_t_test": test,
            "cohens_d": cohens_d(cer_by_model["ThaiTrOCR"], cer_by_model["PP-OCRv5-mobile"]),
        }
    )

    decision = (
        "ThaiTrOCR"
        if thai_mean <= c.OCR_CER_THRESHOLD
        else ("ThaiTrOCR" if thai_mean < pp_mean else "PP-OCRv5-mobile")
    )
    results["decision"] = {
        "rule": f"ThaiTrOCR iff mean CER <= {c.OCR_CER_THRESHOLD}, else lowest CER",
        "mean_cer_thaitrocr": thai_mean,
        "mean_cer_ppocrv5": pp_mean,
        "selected": decision,
    }
    write_json(args.output, results)
    partial_path.unlink(missing_ok=True)

    print(f"ThaiTrOCR mean CER: {thai_mean:.4f} | PP-OCRv5-mobile: {pp_mean:.4f}", flush=True)
    print(f"paired t-test p={test['p_value']:.4f} significant={test['significant']}", flush=True)
    print(f"SELECTED: {decision}", flush=True)
    print(f"wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()
