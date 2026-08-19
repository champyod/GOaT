#!/usr/bin/env python3
"""OCR fine-tuning: Grid Search over LR/batch/epochs + early stopping, then ONNX export.

Placeholder wiring - the actual training callbacks depend on whether the
selected backend is PaddleOCR or a TrOCR (transformers) trainer. Fill the
TODO once the selection experiment pins the winner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.utils import setup_seed, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR fine-tuning grid search (placeholder).")
    parser.add_argument("--model", choices=c.OCR_MODELS, required=True)
    parser.add_argument("--output", type=Path, default=c.RESULTS / "train_ocr.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    setup_seed(args.seed)
    if not c.TRAIN.is_dir() or not c.VAL.is_dir():
        raise SystemExit(f"run scripts/split_data.py first - expected {c.TRAIN} and {c.VAL}")

    print(
        f"grid: LR {c.OCR_GRID_LEARNING_RATES} x batch {c.OCR_GRID_BATCH_SIZES} x epochs {c.OCR_GRID_EPOCHS}"
    )
    print(
        "TODO: implement training loop - Adam, early stopping on val CER (patience "
        f"{c.OCR_EARLY_STOP_PATIENCE}), data augmentation per methodology, "
        "then export the winner to ONNX via scripts/export_models.py"
    )

    write_json(
        args.output,
        {
            "model": args.model,
            "status": "placeholder",
            "grid": {
                "learning_rates": list(c.OCR_GRID_LEARNING_RATES),
                "batch_sizes": list(c.OCR_GRID_BATCH_SIZES),
                "epochs": list(c.OCR_GRID_EPOCHS),
            },
        },
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
