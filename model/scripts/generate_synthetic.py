#!/usr/bin/env python3
"""Download the 10k synthetic OCR dataset from Hugging Face, then 70/15/15 split.

The 10k SynthTIGER-generated images (512x512, 50/50 Thai/English from
Wikipedia, 5 fonts) live in the ``KunanonKhai/Synthetic-GOaT-OCR`` dataset
repository as ``images/<shard>/<idx>.jpg`` plus a tab-separated ``gt.txt``.
This script downloads the repo, flattens it into same-stem ``.png``/``.txt``
pairs, and runs the stratified train/val/test split so the OCR trainer can
read ``{data-root}/train|val|test`` unchanged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import split_ocr
from goat_model.synth_ocr import _build_manifest, flatten_synthetic


def main() -> None:
    parser = argparse.ArgumentParser(description="Download + split synthetic OCR data.")
    parser.add_argument("--out", type=Path, default=c.SYNTHETIC)
    parser.add_argument("--real", type=Path, default=c.REAL)
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    print(f"[args] {args}", flush=True)

    gen_dir = args.out / "gen"
    manifest_path = args.out / "manifest.json"
    if manifest_path.is_file():
        manifest = _build_manifest(args.out)
        print(f"downloaded dataset already present - reusing {args.out}")
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as err:
            raise SystemExit(
                "huggingface_hub not installed - add it to pyproject.toml and re-run `uv sync`, "
                "then this script can fetch the synthetic dataset."
            ) from err

        snapshot_download(
            repo_id=c.OCR_SYNTHETIC_REPO_ID,
            repo_type="dataset",
            local_dir=gen_dir,
        )
        manifest = _build_manifest(args.out)
        flatten_synthetic(gen_dir, manifest, args.out, prefix=c.OCR_SYN_PREFIX)
        print(f"downloaded {len(manifest)} synthetic images -> {args.out}")

    split_ocr(synthetic_dir=args.out, real_dir=args.real, out_root=args.out.parent, seed=args.seed)


if __name__ == "__main__":
    main()