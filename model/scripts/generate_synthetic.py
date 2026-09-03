#!/usr/bin/env python3
"""Synthetic OCR data generation with SynthTIGER (10k images, 512x512).

Parameters: 50/50 Thai/English from Wikipedia, 5 fonts
(TH Sarabun PSK, Noto Sans Thai, Kanit, Prompt, Sriracha), Gaussian noise
sigma 10-30. SynthTIGER configs are data files, not stable Python APIs, so the
exact corpus/font wiring is left as a TODO once SynthTIGER is pinned.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c

FONTS = ["TH Sarabun PSK", "Noto Sans Thai", "Kanit", "Prompt", "Sriracha"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic OCR images with SynthTIGER.")
    parser.add_argument("--n", type=int, default=c.DATA_PLAN["synthetic"])
    parser.add_argument("--out", type=Path, default=c.SYNTHETIC)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--sigma-min", type=float, default=10.0)
    parser.add_argument("--sigma-max", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    try:
        import synthtiger  # noqa: F401
    except ImportError as err:
        raise SystemExit(
            "synthtiger not installed. Add it to pyproject.toml and re-run `uv sync`, "
            "then wire the SynthTIGER corpus/fonts config here."
        ) from err

    print(
        f"generating {args.n} images -> {args.out} (size {args.size}, "
        f"gaussian sigma {args.sigma_min}-{args.sigma_max})"
    )
    print("TODO: SynthTIGER pipeline - 50/50 th/en from Wikipedia, fonts: " + ", ".join(FONTS))
    args.out.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
