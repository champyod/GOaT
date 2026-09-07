#!/usr/bin/env python3
"""MT fine-tuning: LoRA on NLLB-200-distilled-600M (weights frozen), BLEU-per-epoch.

Grid: rank {8,12,16} x alpha {16,32,48}; LR 2e-4..5e-4; epochs 3..10; Adam.
Winner exported to CTranslate2 via scripts/export_models.py. NLLB-1.3B stays
zero-shot (no fine-tune).
"""

from __future__ import annotations

import argparse
import json
import traceback
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.log import dump as _dump
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import load_dotenv, log_call, setup_seed


@log_call
def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="LoRA fine-tuning for NLLB-600M (selection winner).")
    parser.add_argument("--mt-dir", type=Path, default=c.DATA / "mt")
    parser.add_argument("--selection", type=Path, default=c.RESULTS / "mt_selection.json")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "mt_training.json")
    parser.add_argument("--out-root", type=Path, default=c.ART_MT)
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--force", action="store_true", help="ignore existing results, retrain")
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    _info("train-mt", "args", **vars(args))
    _err_out = args.output
    try:
        try:
            import peft  # noqa: F401
        except ImportError as err:
            if err.name not in (None, "peft"):
                raise RuntimeError(
                    f"peft is installed but its import chain is broken at {err.name!r} ({err})"
                    " - re-sync will not help, fix the env (e.g. torch/CUDA/NCCL mismatch)"
                ) from err
            raise RuntimeError("peft not installed - re-run `uv sync --extra train`") from err

        from goat_model.mt.train import run_mt_finetune

        setup_seed(args.seed)
        if args.force:
            args.output.unlink(missing_ok=True)
            args.output.with_name(args.output.stem + ".partial.json").unlink(missing_ok=True)
        run_mt_finetune(mt_dir=args.mt_dir, selection_path=args.selection, result_path=args.output, out_root=args.out_root, seed=args.seed)


    except Exception as err:
        tb = traceback.format_exc()
        inp = args.mt_dir
        _err("train-mt", "failed", inp=str(inp), out=str(_err_out), error=str(err))
        _dump("traceback", tb)
        try:
            _err_path = str(_err_out) + ".error.json"
            from pathlib import Path as _P
            _P(_err_path).write_text(json.dumps({"error": str(err), "kind": "train_mt", "input": str(inp), "output": str(_err_out)}, indent=2))
            _info("train-mt", "wrote error file", path=_err_path)
        except Exception:
            pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
