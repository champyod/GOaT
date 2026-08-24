"""Bare-bones dataset preparation functions.

Each function takes explicit output directories so it works unchanged on a
local machine or against a mounted Drive path in Colab. CLI wrappers live in
model/scripts/; notebooks should import these functions directly.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import numpy as np

from goat_model import constants as c

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

HF_DATASET_REPOS = {
    "scb-mt": "pythainlp/scb_mt_en_th_2020",
    "flores200": "facebook/flores",
    "thaiocrbench": "scb10x/ThaiOCRBench",
    "thai-ocr-evaluation": "openthaigpt/thai-ocr-evaluation",
}


def dataset_revisions() -> dict[str, str | None]:
    """Best-effort commit hash of each backing HF repo, for result provenance."""
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import HfHubHTTPError
    except ImportError as err:
        raise SystemExit("huggingface_hub not installed - run `uv sync --extra mt`") from err

    api = HfApi()
    revisions: dict[str, str | None] = {}
    for name, repo in HF_DATASET_REPOS.items():
        try:
            revisions[name] = api.dataset_info(repo).sha
        except (HfHubHTTPError, OSError):
            revisions[name] = None
    return revisions


def download_flores200(out_dir: Path) -> Path:
    """Download FLORES-200 devtest th/en as parallel one-sentence-per-line files."""
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    ds = load_dataset("facebook/flores", "all", split="devtest")

    th = [row["sentence"] for row in ds if row["id"] == "tha_Thai"]
    en = [row["sentence"] for row in ds if row["id"] == "eng_Latn"]
    if len(th) != len(en):
        raise ValueError(f"parallel corpus broken: {len(th)} th vs {len(en)} en")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flores200.th").write_text("\n".join(th) + "\n", encoding="utf-8")
    (out_dir / "flores200.en").write_text("\n".join(en) + "\n", encoding="utf-8")
    print(f"wrote {len(th)} th + {len(en)} en sentences to {out_dir}")
    return out_dir


def download_scbmt(
    out_dir: Path,
    sample: int = c.SCBMT_SAMPLE,
    train_n: int = c.SCBMT_TRAIN,
    val_n: int = c.SCBMT_VAL,
    test_n: int = c.SCBMT_TEST,
    seed: int = c.SEED,
) -> Path:
    """Stratified-sample SCB-MT-EN-TH into train/val/test .th/.en files."""
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    ds = load_dataset("pythainlp/scb_mt_en_th_2020", split="train")
    print(f"loaded {ds.num_rows} pairs (target sample: {sample})")

    rng = np.random.default_rng(seed)

    strat_col = next((col for col in ("domain", "year_month") if col in ds.features), None)
    if strat_col is None:
        print("WARN: no categorical column for stratification - using plain random sample")
        idx = rng.choice(ds.num_rows, sample, replace=False)
    else:
        cats, counts = np.unique(np.asarray(ds[strat_col]), return_counts=True)
        per = np.floor(sample * counts / ds.num_rows).astype(int)
        leftover = sample - int(per.sum())
        fracs = sample * counts / ds.num_rows - per
        per[np.argsort(-fracs)[:leftover]] += 1
        cols = np.asarray(ds[strat_col])
        picks = [
            rng.choice(np.flatnonzero(cols == cat), int(n), replace=False)
            for cat, n in zip(cats, per)
            if n
        ]
        idx = np.concatenate([p.astype(int) for p in picks])
        print(f"stratified {sample} by {strat_col} ({len(cats)} groups, retained {len(idx)})")

    shuffled = rng.permutation(idx)
    splits = {
        "train": shuffled[:train_n],
        "val": shuffled[train_n : train_n + val_n],
        "test": shuffled[train_n + val_n : train_n + val_n + test_n],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, sel in splits.items():
        sub = ds.select(sel.tolist())
        th = [row["translation"]["th"] for row in sub]
        en = [row["translation"]["en"] for row in sub]
        (out_dir / f"{name}.th").write_text("\n".join(th) + "\n", encoding="utf-8")
        (out_dir / f"{name}.en").write_text("\n".join(en) + "\n", encoding="utf-8")
        print(f"wrote {name} ({len(sel)} pairs) -> {out_dir}/{name}.{{th,en}}")
    return out_dir


def ingest_manual(dataset: str, source: Path, ocr_root: Path = c.OCR_EVAL) -> Path:
    """Copy a manually downloaded OCR dataset into images/ + gt/ canonical layout."""
    dst = ocr_root / dataset
    (dst / "images").mkdir(parents=True, exist_ok=True)
    (dst / "gt").mkdir(parents=True, exist_ok=True)

    imgs = sorted(p for p in source.rglob("*") if p.suffix.lower() in IMG_EXTS)
    if not imgs:
        raise SystemExit(f"no images found under {source}")
    gt_map: dict[str, Path] = {}
    for gt in source.rglob("*.txt"):
        gt_map[gt.stem] = gt

    copied = matched_gt = 0
    for img in imgs:
        shutil.copy2(img, dst / "images" / img.name)
        copied += 1
        gt = gt_map.get(img.stem)
        if gt is not None:
            shutil.copy2(gt, dst / "gt" / f"{img.stem}.txt")
            matched_gt += 1

    missing = len(imgs) - matched_gt
    print(f"copied {copied} images; {matched_gt} had ground truth; {missing} MISSING GT")
    if missing:
        raise SystemExit("please provide a .txt with the same stem for every image")
    return dst


def _export_image_gt(items, out_dir: Path) -> Path:
    """Write (stem, PIL image, text) triples into the images/ + gt/ canonical layout."""
    images_dir = out_dir / "images"
    gt_dir = out_dir / "gt"
    images_dir.mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for stem, image, text in items:
        image.save(images_dir / f"{stem}.png")
        (gt_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
        count += 1
    if not count:
        raise SystemExit(f"no exportable rows for {out_dir.name}")
    print(f"wrote {count} images+gt to {out_dir}")
    return out_dir


def download_thaiocr_evaluation(out_dir: Path) -> Path:
    """openthaigpt/thai-ocr-evaluation test split (104 imgs): image/text -> canonical layout."""
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    ds = load_dataset("openthaigpt/thai-ocr-evaluation", split="test")
    items = ((f"{i:04d}", row["image"], str(row["text"])) for i, row in enumerate(ds))
    return _export_image_gt(items, out_dir)


def download_thaiocrbench(out_dir: Path) -> Path:
    """scb10x/ThaiOCRBench test split: whole-image transcription tasks -> canonical layout.

    Keeps only 'Text recognition' and 'Full-page OCR' rows - the paper defines both as
    full transcriptions of the image, comparable via CER. Region/extraction/VQA tasks
    are skipped because their answers are not whole-image text.
    """
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise SystemExit("datasets not installed - run `uv sync --extra mt`") from err

    ds = load_dataset("scb10x/ThaiOCRBench", split="test")
    wanted = {"text recognition", "full-page ocr"}
    items = (
        (str(row["Id"]), row["image"], str(row["answer"]).strip())
        for row in ds
        if str(row["Task"]).strip().lower() in wanted
    )
    return _export_image_gt(items, out_dir)


def split_ocr(
    synthetic_dir: Path = c.SYNTHETIC,
    real_dir: Path = c.REAL,
    out_root: Path = c.DATA,
    seed: int = c.SEED,
) -> dict[str, int]:
    """Stratified 70/15/15 split of synthetic+real OCR assets with a manifest."""
    def _assets(root: Path) -> list[tuple[Path, Path]]:
        result: list[tuple[Path, Path]] = []
        for img in sorted(root.glob("*.png")) + sorted(root.glob("*.jpg")):
            gt = root / f"{img.stem}.txt"
            if gt.is_file():
                result.append((img, gt))
        return result

    rng = random.Random(seed)
    synthetic = _assets(synthetic_dir)
    real = _assets(real_dir)
    if not synthetic:
        raise SystemExit(f"no synthetic images+gt in {synthetic_dir}")
    if not real:
        raise SystemExit(f"no real images+gt in {real_dir}")

    print(f"found {len(synthetic)} synthetic, {len(real)} real images")

    def split_group(group: list[tuple[Path, Path]], prefix: str) -> dict[str, list]:
        rng.shuffle(group)
        n_train = round(len(group) * c.TRAIN_RATIO)
        n_val = round(len(group) * c.VAL_RATIO)
        return {
            f"{prefix}_train": group[:n_train],
            f"{prefix}_val": group[n_train : n_train + n_val],
            f"{prefix}_test": group[n_train + n_val :],
        }

    parts = split_group(synthetic, "syn")
    parts.update(split_group(real, "real"))

    destination = {
        name: out_root / name.split("_")[1]
        for name in ("syn_train", "syn_val", "syn_test", "real_train", "real_val", "real_test")
    }
    manifest: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for key, items in parts.items():
        dest = destination[key]
        dest.mkdir(parents=True, exist_ok=True)
        manifest.setdefault(dest.name, [])
        for img, gt in items:
            shutil.copy2(img, dest / img.name)
            shutil.copy2(gt, dest / gt.name)
            manifest[dest.name].append(img.name)
        counts[key] = len(items)

    from goat_model.utils import write_json

    write_json(out_root / "split_manifest.json", {"seed": seed, "counts": counts, "files": manifest})

    totals: dict[str, int] = {}
    for split in ("train", "val", "test"):
        syn = counts.get(f"syn_{split}", 0)
        real_n = counts.get(f"real_{split}", 0)
        totals[split] = syn + real_n
        print(f"  {split}: {totals[split]} ({syn} syn, {real_n} real)")
    print(f"expected: {c.DATA_PLAN['train']}/{c.DATA_PLAN['val']}/{c.DATA_PLAN['test']}")
    return totals
