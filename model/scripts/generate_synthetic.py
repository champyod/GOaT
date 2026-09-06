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
from goat_model.utils import log_call


@log_call
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
    print(f"[step] synthetic: rebuilding manifest from {gen_dir} ...", flush=True)
    manifest = _build_manifest(args.out) if manifest_path.is_file() else {}
    if manifest:
        print(f"[step] synthetic: verifying {len(manifest)} manifest entries ...", flush=True)
        bad = [
            key for key in manifest
            if not (gen_dir / key).is_file() or (gen_dir / key).stat().st_size == 0
        ]
        print(f"[step] synthetic: {len(manifest) - len(bad)}/{len(manifest)} entries ok", flush=True)
        if bad:
            print(f"[fetch] {len(bad)} missing/corrupt entries, re-downloading ...", flush=True)
            manifest = {}
    if manifest:
        print(f"downloaded dataset already present - reusing {args.out} ({len(manifest)} images)")
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as err:
            raise SystemExit(
                "huggingface_hub not installed - add it to pyproject.toml and re-run `uv sync`, "
                "then this script can fetch the synthetic dataset."
            ) from err

        import threading
        import time

        from goat_model.utils import LogProgress

        gen_dir.mkdir(parents=True, exist_ok=True)
        try:
            from huggingface_hub import HfApi
            info = HfApi().dataset_info(c.OCR_SYNTHETIC_REPO_ID)
            total_mb = max(1, int((info.usedStorage or 0) // 1048576))
        except Exception:
            total_mb = 0
        fetch_prog = (
            LogProgress(total_mb, "fetch", unit="MB", interval_s=3.0, in_path=c.OCR_SYNTHETIC_REPO_ID, out_path=str(gen_dir))
            if total_mb else None
        )
        stop = threading.Event()

        def _fetch_watch():
            t0 = time.monotonic()
            while not stop.wait(3.0):
                try:
                    size = sum(p.stat().st_size for p in gen_dir.rglob("*") if p.is_file())
                except Exception:
                    size = 0
                if fetch_prog is not None:
                    fetch_prog.update(max(0, size // 1048576 - fetch_prog.n))
                else:
                    print(f"[fetch] downloading {c.OCR_SYNTHETIC_REPO_ID} ... {size / 1048576:.0f}MB elapsed={time.monotonic() - t0:.0f}s -> {gen_dir}", flush=True)

        watcher = threading.Thread(target=_fetch_watch, daemon=True)
        watcher.start()
        print(f"[step] synthetic: snapshot download {c.OCR_SYNTHETIC_REPO_ID} -> {gen_dir} ...", flush=True)
        last_err: Exception | None = None
        for attempt in range(1, 7):
            try:
                snapshot_download(
                    repo_id=c.OCR_SYNTHETIC_REPO_ID,
                    repo_type="dataset",
                    local_dir=gen_dir,
                )
                last_err = None
                break
            except Exception as err:
                if not isinstance(err, (ConnectionError, TimeoutError, OSError)) and "429" not in str(err):
                    raise
                last_err = err
                wait = 30 * attempt
                print(f"[fetch] attempt {attempt}/6 failed ({err}). retry in {wait}s ...", flush=True)
                time.sleep(wait)
        stop.set()
        watcher.join(timeout=1.0)
        if fetch_prog is not None:
            fetch_prog.close()
        if last_err is not None:
            raise last_err
        manifest = _build_manifest(args.out)
        if not manifest:
            raise SystemExit(
                f"no gt.txt entries under {gen_dir} - check {c.OCR_SYNTHETIC_REPO_ID} "
                "holds images/<shard>/<idx>.jpg plus tab-separated gt.txt at root"
            )
        flatten_synthetic(gen_dir, manifest, args.out, prefix=c.OCR_SYN_PREFIX)
        print(f"downloaded {len(manifest)} synthetic images -> {args.out}")

    print(f"[step] synthetic: stratified split -> {args.out.parent} ...", flush=True)
    split_ocr(synthetic_dir=args.out, real_dir=args.real, out_root=args.out.parent, seed=args.seed)
    print("[step] synthetic: done", flush=True)


if __name__ == "__main__":
    main()