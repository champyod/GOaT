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
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import LogProgress, load_dotenv, log_call

TAR_FILE = "synth.tar"
TAR_REVISION = "tar"


@log_call
def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Download + split synthetic OCR data.")
    parser.add_argument("--out", type=Path, default=c.SYNTHETIC)
    parser.add_argument("--real", type=Path, default=c.REAL)
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    _info("synthetic", "args", **vars(args))

    gen_dir = args.out / "gen"
    manifest_path = args.out / "manifest.json"
    _info("synthetic", "rebuilding manifest", gen=str(gen_dir))
    manifest = _build_manifest(args.out) if manifest_path.is_file() else {}
    if manifest:
        verify_prog = LogProgress(len(manifest), "verify", unit="entries", interval_s=3.0, in_path=str(gen_dir))
        bad = []
        for key in manifest:
            try:
                ok = (gen_dir / key).stat().st_size > 0
            except OSError:
                ok = False
            if not ok:
                bad.append(key)
            verify_prog.update()
        verify_prog.close()
        _info("synthetic", "entries ok", ok=len(manifest) - len(bad), total=len(manifest))
        if bad:
            _warn("fetch", "missing/corrupt entries, re-downloading", bad=len(bad))
            manifest = {}
    if manifest:
        _info("synthetic", "dataset present - reusing", out=str(args.out), images=len(manifest))
    else:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as err:
            raise SystemExit(
                "huggingface_hub not installed - add it to pyproject.toml and re-run `uv sync`, "
                "then this script can fetch the synthetic dataset."
            ) from err

        import tarfile

        _info("synthetic", "tar download", repo=c.OCR_SYNTHETIC_REPO_ID, revision=TAR_REVISION, file=TAR_FILE)
        tar_path = hf_hub_download(
            repo_id=c.OCR_SYNTHETIC_REPO_ID,
            repo_type="dataset",
            revision=TAR_REVISION,
            filename=TAR_FILE,
        )
        gen_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r") as tf:
            tf.extractall(gen_dir)
        manifest = _build_manifest(args.out)
        if not manifest:
            raise SystemExit(
                f"no gt.txt entries under {gen_dir} - check {c.OCR_SYNTHETIC_REPO_ID} "
                "holds images/<shard>/<idx>.jpg plus tab-separated gt.txt at root"
            )
        flatten_synthetic(gen_dir, manifest, args.out, prefix=c.OCR_SYN_PREFIX)
        _info("synthetic", "downloaded", images=len(manifest), out=str(args.out))

    _info("synthetic", "stratified split", out=str(args.out.parent))
    split_ocr(synthetic_dir=args.out, real_dir=args.real, out_root=args.out.parent, seed=args.seed)
    _info("synthetic", "done")


if __name__ == "__main__":
    main()