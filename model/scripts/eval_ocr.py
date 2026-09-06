#!/usr/bin/env python3
"""OCR evaluation.

Usage:
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
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, resolve_device, setup_seed, write_json


@log_call
def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an OCR model on a dataset.")
    parser.add_argument("--model", choices=c.OCR_MODELS, default="PP-OCRv5-mobile")
    parser.add_argument("--dataset", choices=c.OCR_DATASETS, default="thaiocrbench")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda", help="cuda (default, fails fast if unavailable) | cpu (explicit, slow)")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    args = parser.parse_args()
    args.device = resolve_device(args.device)

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

    backend = get_ocr(args.model, device=args.device, seed=args.seed)
    img_size = c.OCR_IMG_SIZE[args.model]
    _info("eval-ocr", "loaded images", model=args.model, images=len(assets), dataset=args.dataset, device=args.device)

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
            _info("eval-ocr", "metric", metric=metric, mean=mean, std=std, ci_low=round(ci[0], 4), ci_high=round(ci[1], 4))
    _info("eval-ocr", "wrote", out=str(args.output))


if __name__ == "__main__":
    main()
