#!/usr/bin/env python3
"""Synthetic OCR data generation with SynthTIGER (10k images, 512x512).

Parameters: 50/50 Thai/English from Wikipedia, 5 fonts
(TH Sarabun PSK, Noto Sans Thai, Kanit, Prompt, Sriracha), Gaussian noise
sigma 10-30. Skips when the flattened synthetic dataset already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.synth_ocr import (
    download_ocr_fonts,
    fetch_wikipedia_corpus,
    flatten_synthetic,
    generate_synthtiger,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic OCR images with SynthTIGER.")
    parser.add_argument("--n", type=int, default=c.DATA_PLAN["synthetic"])
    parser.add_argument("--out", type=Path, default=c.SYNTHETIC)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--fonts", type=Path, default=None)
    parser.add_argument("--size", type=int, default=c.OCR_SIZE)
    parser.add_argument("--sigma-min", type=float, default=c.OCR_NOISE_SIGMA[0])
    parser.add_argument("--sigma-max", type=float, default=c.OCR_NOISE_SIGMA[1])
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    if (args.out / "manifest.json").is_file():
        print(f"skipped - synthetic dataset already exists: {args.out}")
        return

    try:
        import synthtiger  # noqa: F401
    except ImportError as err:
        raise SystemExit(
            "synthtiger not installed. Add it to pyproject.toml and re-run `uv sync`, "
            "then wire the SynthTIGER corpus/fonts config here."
        ) from err

    corpus_dir = args.corpus or args.out / "corpus"
    font_dir = args.fonts or args.out / "fonts"

    corpus = fetch_wikipedia_corpus(corpus_dir, c.OCR_WIKI_LANGS)
    download_ocr_fonts(font_dir, c.OCR_FONTS)

    gen_dir = args.out / "gen"
    manifest = generate_synthtiger(
        out_dir=gen_dir,
        thai_corpus=corpus["th"],
        english_corpus=corpus["en"],
        font_dir=font_dir,
        n=args.n,
        workers=c.OCR_HW_CORES,
        seed=args.seed,
        text_ratio=c.OCR_TEXT_RATIO,
        noise_sigma=(args.sigma_min, args.sigma_max),
    )
    flatten_synthetic(gen_dir, manifest, args.out, prefix=c.OCR_SYN_PREFIX)
    print(f"generated {len(manifest)} synthetic images -> {args.out}")


if __name__ == "__main__":
    main()
