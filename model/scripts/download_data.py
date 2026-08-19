#!/usr/bin/env python3
"""Dataset download / ingestion into the canonical layout.

Supports:
  --dataset scb-mt        SCB-MT-EN-TH (HF pythainlp/scb_mt_en_th_2020) -> data/mt/train.csv
  --dataset flores200     FLORES-200 th/en -> data/mt/test/flores200.th|.en
  --dataset thaiocrbench <MANUAL> canonical layout only (source TBD, see paper thaiocrbench2025)
  --dataset thai-ocr-evaluation <MANUAL> canonical layout only (source TBD, see paper thaitrocr2024)

Manual datasets: place files yourself, then run `--dataset X --ingest DIR` to copy
them into data/ocr/<dataset>/{images,gt}. No download URL is guessed here - confirm
the canonical source from the papers before adding an automated downloader.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c


def _download_flores200() -> None:
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    dataset = load_dataset("facebook/flores", "all", split="devtest")
    out = c.MT_TEST
    out.mkdir(parents=True, exist_ok=True)

    th_lines = [row["sentence"] for row in dataset if row["id"] == "tha_Thai"]
    en_lines = [row["sentence"] for row in dataset if row["id"] == "eng_Latn"]
    if len(th_lines) != len(en_lines):
        print(f"WARN: th={len(th_lines)} vs en={len(en_lines)} - pairing by position only")
    th_lines.sort()
    en_lines.sort()

    (out / "flores200.th").write_text("\n".join(th_lines) + "\n", encoding="utf-8")
    (out / "flores200.en").write_text("\n".join(en_lines) + "\n", encoding="utf-8")
    print(f"wrote {len(th_lines)} th + {len(en_lines)} en sentences to {out}")


def _download_scbmt() -> None:
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    ds = load_dataset("pythainlp/scb_mt_en_th_2020", split="train")
    print(f"loaded {ds.num_rows} pairs (target sample: {c.SCBMT_SAMPLE})")

    rng = np.random.default_rng(c.SEED)

    strat_col = next((col for col in ("domain", "year_month") if col in ds.features), None)
    if strat_col is None:
        print("WARN: no categorical column for stratification - using plain random sample")
        idx = rng.choice(ds.num_rows, c.SCBMT_SAMPLE, replace=False)
    else:
        cats, counts = np.unique(np.asarray(ds[strat_col]), return_counts=True)
        per = np.floor(c.SCBMT_SAMPLE * counts / ds.num_rows).astype(int)
        leftover = c.SCBMT_SAMPLE - int(per.sum())
        fracs = c.SCBMT_SAMPLE * counts / ds.num_rows - per
        per[np.argsort(-fracs)[:leftover]] += 1
        cols = np.asarray(ds[strat_col])
        picks = [
            rng.choice(np.flatnonzero(cols == cat), int(n), replace=False)
            for cat, n in zip(cats, per)
            if n
        ]
        idx = np.concatenate([p.astype(int) for p in picks])
        print(
            f"stratified {c.SCBMT_SAMPLE} by {strat_col} ({len(cats)} groups, retained {len(idx)})"
        )

    shuffled = rng.permutation(idx)
    train_i = shuffled[: c.SCBMT_TRAIN]
    val_i = shuffled[c.SCBMT_TRAIN : c.SCBMT_TRAIN + c.SCBMT_VAL]
    test_i = shuffled[c.SCBMT_TRAIN + c.SCBMT_VAL : c.SCBMT_TRAIN + c.SCBMT_VAL + c.SCBMT_TEST]

    for name, sel in (("train", train_i), ("val", val_i), ("test", test_i)):
        _write_mt_split(name, sel, ds)


def _write_mt_split(name: str, indices: np.ndarray, ds) -> None:
    sub = ds.select(indices.tolist())
    th = [row["translation"]["th"] for row in sub]
    en = [row["translation"]["en"] for row in sub]
    out = c.DATA / "mt"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.th").write_text("\n".join(th) + "\n", encoding="utf-8")
    (out / f"{name}.en").write_text("\n".join(en) + "\n", encoding="utf-8")
    print(f"wrote {name} ({len(indices)} pairs) -> {out}/{name}.{{th,en}}")


def _ingest_manual(dataset: str, source: Path) -> None:
    dst = c.OCR_EVAL / dataset
    (dst / "images").mkdir(parents=True, exist_ok=True)
    (dst / "gt").mkdir(parents=True, exist_ok=True)

    imgs = sorted(
        p
        for p in source.rglob("*")
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    )
    if not imgs:
        raise SystemExit(f"no images found under {source}")
    gt_map: dict[str, Path] = {}
    for gt in source.rglob("*.txt"):
        gt_map[gt.stem] = gt

    copied = matched_gt = 0
    for img in imgs:
        to = dst / "images" / img.name
        shutil.copy2(img, to)
        copied += 1
        gt = gt_map.get(img.stem)
        if gt is not None:
            shutil.copy2(gt, dst / "gt" / f"{img.stem}.txt")
            matched_gt += 1

    missing = len(imgs) - matched_gt
    print(f"copied {copied} images; {matched_gt} had ground truth; {missing} MISSING GT")
    if missing:
        raise SystemExit("please provide a .txt with the same stem for every image")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download/ingest GOaT datasets.")
    parser.add_argument(
        "--dataset",
        choices=("scb-mt", "flores200", "thaiocrbench", "thai-ocr-evaluation"),
        required=True,
    )
    parser.add_argument("--ingest", type=Path, help="dir of images+gt for the manual OCR datasets")
    args = parser.parse_args()

    if args.dataset == "flores200":
        _download_flores200()
    elif args.dataset == "scb-mt":
        _download_scbmt()
    else:
        if args.ingest is None:
            raise SystemExit(
                f"{args.dataset} has no automated download yet - pass --ingest <dir> "
                "containing the images plus a same-stem .txt ground truth per image"
            )
        _ingest_manual(args.dataset, args.ingest)


if __name__ == "__main__":
    main()
