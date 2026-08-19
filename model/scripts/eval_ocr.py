#!/usr/bin/env python3
"""OCR evaluation.

Usage (from analysis.typ):
    python scripts/eval_ocr.py --model PP-OCRv5-mobile --dataset thaiocrbench \
        --output ./results/ocr.json --device cpu --seed 42
    python scripts/eval_ocr.py --model ThaiTrOCR --dataset thai-ocr-evaluation ...

Runs `--repeats` (5) full passes over the dataset and reports per-metric
mean, std and 95% bootstrap CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.ocr import evaluate
from goat_model.ocr.engine import get_ocr
from goat_model.utils import setup_seed, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an OCR model on a dataset.")
    parser.add_argument("--model", choices=c.OCR_MODELS, default="PP-OCRv5-mobile")
    parser.add_argument("--dataset", choices=c.OCR_DATASETS, default="thaiocrbench")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    args = parser.parse_args()

    setup_seed(args.seed)
    dataset_dir = c.OCR_EVAL / args.dataset
    try:
        assets = evaluate.discover_assets(dataset_dir)
    except FileNotFoundError as err:
        parser.error(f"{err} - run scripts/download_data.py --dataset {args.dataset}")
    if not assets:
        parser.error(
            f"no images under {dataset_dir} - run scripts/download_data.py --dataset {args.dataset}"
        )

    backend = get_ocr(args.model, device=args.device)
    img_size = c.OCR_IMG_SIZE[args.model]
    print(f"[{args.model}] {len(assets)} images from {args.dataset} on {args.device}")

    runs = [
        evaluate.run_ocr(backend, assets, img_size, seed=args.seed) for _ in range(args.repeats)
    ]
    summary = evaluate.aggregate_records(runs)

    report = {
        "model": args.model,
        "dataset": args.dataset,
        "device": args.device,
        "seed": args.seed,
        "n_runs": args.repeats,
        "n_images": len(assets),
        "metrics": summary,
        "runs": runs,
    }
    write_json(args.output, report)

    for metric, vals in summary.items():
        if isinstance(vals, dict) and "mean" in vals:
            mean = f"{vals['mean']:.4f}".rstrip("0").rstrip(".")
            std = f"{vals['std']:.4f}".rstrip("0").rstrip(".")
            ci = (vals["ci95"]["ci_low"], vals["ci95"]["ci_high"])
            print(f"  {metric:>14}: {mean} ± {std}  95%CI=({ci[0]:.4f}, {ci[1]:.4f})")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
