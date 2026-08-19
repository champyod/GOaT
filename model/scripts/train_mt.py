#!/usr/bin/env python3
"""MT fine-tuning: LoRA on NLLB-200-distilled-600M (weights frozen), BLEU-per-epoch.

Grid: rank {8,12,16} x alpha {16,32,48}; LR 2e-4..5e-4; epochs 3..10; Adam.
Winner exported to CTranslate2 via scripts/export_models.py. NLLB-1.3B stays
zero-shot (no fine-tune).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.mt.engine import NLLB_HF_IDS
from goat_model.utils import setup_seed, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="LoRA fine-tuning for NLLB-600M (placeholder).")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "train_mt.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    setup_seed(args.seed)
    for part in ("train", "val", "test"):
        if not (c.DATA / "mt" / part).is_dir():
            raise SystemExit(
                f"run scripts/download_data.py --dataset scb-mt first (missing data/mt/{part})"
            )

    try:
        import peft  # noqa: F401
    except ImportError as err:
        raise SystemExit("peft not installed - re-run `uv sync --extra train`") from err

    print(
        f"LoRA grid: r={c.LORA_RANKS} alpha={c.LORA_ALPHAS} LR={c.LORA_LEARNING_RATES} epochs={c.LORA_EPOCHS}"
    )
    print(
        f"base model: {NLLB_HF_IDS['NLLB-200-distilled-600M']} (weights frozen, target modules "
        f"{c.LORA_TARGET_MODULES}); track BLEU on FLORES-200 every epoch"
    )
    print(
        "TODO: implement the training loop; export winner to CTranslate2 via scripts/export_models.py"
    )

    write_json(
        args.output,
        {
            "status": "placeholder",
            "grid": {
                "ranks": list(c.LORA_RANKS),
                "alphas": list(c.LORA_ALPHAS),
                "learning_rates": list(c.LORA_LEARNING_RATES),
                "epochs": list(c.LORA_EPOCHS),
                "target_modules": list(c.LORA_TARGET_MODULES),
            },
        },
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
