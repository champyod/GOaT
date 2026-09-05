#!/usr/bin/env python3
"""OCR fine-tuning: Grid Search over LR/batch/epochs + early stopping, then ONNX export.

Reads the selection winner; only ThaiTrOCR is fine-tuned (full weights),
PP-OCRv5-mobile stays frozen. All training logic lives in goat_model.ocr.train.
"""

from __future__ import annotations

import argparse
import traceback
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.utils import setup_seed


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR fine-tuning for ThaiTrOCR (selection winner).")
    parser.add_argument("--selection", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--data-root", type=Path, default=c.DATA / "ocr")
    parser.add_argument("--out-root", type=Path, default=c.ART_OCR)
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_training.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--force", action="store_true", help="ignore existing results, retrain")
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    print(f"[args] {args}", flush=True)
    _err_out = args.output
    try:

        setup_seed(args.seed)
        if args.force:
            args.output.unlink(missing_ok=True)
            args.output.with_name(args.output.stem + ".partial.json").unlink(missing_ok=True)
        if args.selection.is_file():
            selected = json.loads(args.selection.read_text()).get("selected", "ThaiTrOCR")
        else:
            selected = "ThaiTrOCR"
        if not selected:
            raise SystemExit(
                f"selection file {args.selection} has no winner "
                "(missing/null 'selected') - rerun select_ocr first, not --force training"
            )
        from goat_model.ocr.train import run_ocr_finetune


        run_ocr_finetune(data_root=args.data_root, out_root=args.out_root, result_path=args.output, selected_model=selected, seed=args.seed)


    except Exception as err:
        tb = traceback.format_exc()
        inp = args.data_root
        print(f"[error] train_ocr failed | in={inp} out={_err_out} | {err}", flush=True)
        print(tb, flush=True)
        if _err_out is not None:
            try:
                _err_path = str(_err_out) + ".error.json"
                from pathlib import Path as _P
                _P(_err_path).write_text(json.dumps({"error": str(err), "kind": "train_ocr", "input": str(inp), "output": str(_err_out)}, indent=2))
                print(f"[error] wrote {{_err_path}}", flush=True)
            except Exception:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
