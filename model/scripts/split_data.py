#!/usr/bin/env python3
"""CLI wrapper around goat_model.data.split_ocr - all logic lives in the library."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model.data import split_ocr


def main() -> None:
    parser = argparse.ArgumentParser(description="Stratified 70/15/15 OCR split.")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    kwargs = {"seed": args.seed} if args.seed is not None else {}
    split_ocr(**kwargs)


if __name__ == "__main__":
    main()
