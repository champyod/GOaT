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
from goat_model.utils import LogProgress, copy_replace, load_dotenv, log_call


def _sync_tree(src: Path, dst: Path) -> tuple[int, int]:
    """Copy only new/changed files src -> dst (size-compare). Returns (copied, skipped)."""
    copied = skipped = 0
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        d = dst / f.relative_to(src)
        if d.is_file() and d.stat().st_size == f.stat().st_size:
            skipped += 1
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        copy_replace(f, d)
        copied += 1
    _info("sync", "tree synced", copied=copied, skipped=skipped, src=str(src), dst=str(dst))
    return copied, skipped


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
            from huggingface_hub import snapshot_download
        except ImportError as err:
            raise SystemExit(
                "huggingface_hub not installed - add it to pyproject.toml and re-run `uv sync`, "
                "then this script can fetch the synthetic dataset."
            ) from err

        import time
        from concurrent.futures import ThreadPoolExecutor
        import concurrent.futures as _futures

        # Stage in /tmp (fast local writes for 10k files), bulk-copy to Drive after.
        # stage_dir persists across runs so snapshot_download resumes partials.
        stage_dir = Path("/tmp/synth_dl")
        stage_dir.mkdir(parents=True, exist_ok=True)
        if gen_dir.is_dir():
            _info("synthetic", "merging Drive progress", src=str(gen_dir), dst=str(stage_dir))
            _sync_tree(gen_dir, stage_dir)
        try:
            from huggingface_hub import HfApi
            info = HfApi().dataset_info(c.OCR_SYNTHETIC_REPO_ID)
            total_mb = max(1, int((info.usedStorage or 0) // 1048576))
        except Exception:
            total_mb = 0
        fetch_prog = (
            LogProgress(total_mb, "fetch", unit="MB", interval_s=3.0, in_path=c.OCR_SYNTHETIC_REPO_ID, out_path=str(stage_dir))
            if total_mb else None
        )
        _info("synthetic", "snapshot download", repo=c.OCR_SYNTHETIC_REPO_ID, dst=str(stage_dir))
        STALL_AFTER = 120.0
        MAX_ATTEMPTS = 10
        last_err: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            dl = {"size": 0, "moved": time.monotonic(), "t0": time.monotonic()}
            ex = ThreadPoolExecutor(max_workers=1)

            def _run_snapshot():
                snapshot_download(
                    repo_id=c.OCR_SYNTHETIC_REPO_ID,
                    repo_type="dataset",
                    local_dir=stage_dir,
                    # 10k small files x default workers bursts the token endpoint
                    # into 429s; 2 workers after 4-worker bursts still throttled.
                    max_workers=2,
                )

            fut = ex.submit(_run_snapshot)
            err: Exception | None = None
            stalled = False
            while True:
                try:
                    fut.result(timeout=3.0)
                    break
                except _futures.TimeoutError:
                    pass
                except Exception as e:
                    err = e
                    break
                try:
                    size = sum(p.stat().st_size for p in stage_dir.rglob("*") if p.is_file())
                except Exception:
                    size = 0
                now = time.monotonic()
                if size > dl["size"]:
                    dl["size"] = size
                    dl["moved"] = now
                if fetch_prog is not None:
                    fetch_prog.update(max(0, size // 1048576 - fetch_prog.n))
                else:
                    _info("fetch", "downloading", repo=c.OCR_SYNTHETIC_REPO_ID, mb=round(size / 1048576),
                          elapsed=round(now - dl["t0"]), dst=str(stage_dir))
                if not fut.done() and now - dl["moved"] > STALL_AFTER:
                    _warn("fetch", "stalled - abandoning attempt", stall_s=round(STALL_AFTER),
                          mb=round(size / 1048576), attempt=f"{attempt}/{MAX_ATTEMPTS}")
                    stalled = True
                    break
            if stalled:
                # Leaked attempt thread may still write; snapshot moves finished
                # files atomically so duplicates stay valid, newest wins.
                ex.shutdown(wait=False, cancel_futures=True)
                last_err = TimeoutError(f"fetch stalled {STALL_AFTER:.0f}s with no growth")
            else:
                ex.shutdown(wait=True)
                last_err = err
                if last_err is None:
                    break
                if not isinstance(last_err, (ConnectionError, TimeoutError, OSError)) and "429" not in str(last_err):
                    raise last_err
                wait = 30 * attempt
                _warn("fetch", "attempt failed", attempt=f"{attempt}/{MAX_ATTEMPTS}", error=str(last_err), retry_s=wait)
                time.sleep(wait)
        if fetch_prog is not None:
            fetch_prog.close()
        if last_err is not None:
            raise last_err
        _info("synthetic", "staging", src=str(stage_dir), dst=str(gen_dir))
        gen_dir.mkdir(parents=True, exist_ok=True)
        _sync_tree(stage_dir, gen_dir)
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