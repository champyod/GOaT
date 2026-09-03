#!/usr/bin/env python3
"""OCR model selection: PP-OCRv5-mobile vs ThaiTrOCR.

Runs both models on both public datasets for `--repeats` runs, reports
mean±std + 95% CI, then applies the decision rule (hypothesis 2 / methodology):
pick ThaiTrOCR iff its CER <= 0.10, otherwise pick the lowest CER model.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import dataset_revisions
from goat_model.metrics import cohens_d, paired_t_test
from goat_model.ocr import evaluate
from goat_model.ocr.engine import get_ocr
from goat_model.utils import LogProgress, setup_seed, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR selection experiment (CER decision rule).")
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    parser.add_argument("--ocr-eval-dir", type=Path, default=c.OCR_EVAL)
    parser.add_argument("--device", default="auto", help="auto=cuda if available else cpu")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

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
    for model in c.OCR_MODELS:
        stats = {}
        for dataset in c.OCR_DATASETS:
            dataset_dir = args.ocr_eval_dir / dataset
            assets = evaluate.discover_assets(dataset_dir)
            backend = get_ocr(model, device=device, seed=args.seed)
            img_size = c.OCR_IMG_SIZE[model]
            print(f"[select-ocr] {model}/{dataset} — loading weights (first run downloads GBs)", flush=True)
            prog = LogProgress(args.repeats, f"select-ocr {model}/{dataset}", unit="repeat", interval_s=30.0)
            runs = []
            for _ in range(args.repeats):
                runs.append(evaluate.run_ocr(backend, assets, img_size, seed=args.seed))
                prog.update()
            prog.close()
            summary = evaluate.aggregate_records(runs)
            stats[dataset] = summary
            cer_by_model.setdefault(model, []).extend(rec["cer"] for run in runs for rec in run)
        results["models"][model] = stats

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

    print(f"ThaiTrOCR mean CER: {thai_mean:.4f} | PP-OCRv5-mobile: {pp_mean:.4f}", flush=True)
    print(f"paired t-test p={test['p_value']:.4f} significant={test['significant']}", flush=True)
    print(f"SELECTED: {decision}", flush=True)
    print(f"wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()
