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
    parser = argparse.ArgumentParser(description="OCR fine-tuning for ThaiTrOCR (selection winner).")
    parser.add_argument("--selection", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--data-root", type=Path, default=c.DATA / "ocr")
    parser.add_argument("--out-root", type=Path, default=c.ART_OCR)
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_training.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    setup_seed(args.seed)
    from goat_model.ocr.train import run_ocr_finetune
    import json

    if args.selection.is_file():
        selected = json.loads(args.selection.read_text()).get("selected", "ThaiTrOCR")
    else:
        selected = "ThaiTrOCR"

    run_ocr_finetune(data_root=args.data_root, out_root=args.out_root, result_path=args.output, selected_model=selected, seed=args.seed)


if __name__ == "__main__":
    main()
