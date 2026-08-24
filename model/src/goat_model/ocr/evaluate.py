"""OCR evaluation: leak-free per-image metrics across repeat runs.

Canonical dataset layout (created by scripts/download_data.py / split_data.py):

    data/ocr/<dataset>/images/<stem>.png
    data/ocr/<dataset>/gt/<stem>.txt      (same stem, one line = GT text)

Models get a bilinear resize to their native input size before inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from goat_model.constants import SEED
from goat_model.ocr.engine import OCRBackend

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


@dataclass
class OCRAsset:
    image: Path
    gt: Path
    domain: str | None = None


def discover_assets(root: Path) -> list[OCRAsset]:
    images_dir = root / "images"
    gt_dir = root / "gt"
    if not images_dir.is_dir():
        raise FileNotFoundError(f"no images/ dir under {root}")
    assets: list[OCRAsset] = []
    for img in sorted(images_dir.iterdir()):
        if img.suffix.lower() not in IMG_EXTS:
            continue
        gt = gt_dir / f"{img.stem}.txt"
        if not gt.is_file():
            raise FileNotFoundError(f"missing ground truth for {img}")
        assets.append(OCRAsset(image=img, gt=gt))
    return assets


def preprocess(image: np.ndarray, size: int) -> np.ndarray:
    return cv2.resize(image, (size, size), interpolation=cv2.INTER_LINEAR)


def _read_rgb(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot decode image: {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def run_ocr(
    backend: OCRBackend,
    assets: list[OCRAsset],
    img_size: int,
    seed: int = SEED,
) -> list[dict]:
    from goat_model.metrics import cer, word_accuracy
    from goat_model.utils import read_gt, setup_seed

    setup_seed(seed)
    records: list[dict] = []
    for asset in assets:
        image = _read_rgb(asset.image)
        image = preprocess(image, img_size)
        gt_text = read_gt(asset.gt)

        result = backend.recognize(image)
        records.append(
            {
                "image": asset.image.name,
                "reference": gt_text,
                "hypothesis": result.text,
                "cer": cer(gt_text, result.text),
                "word_accuracy": word_accuracy(gt_text, result.text),
                "latency_ms": result.latency_ms,
                "domain": asset.domain,
            }
        )
    return records


def aggregate_records(runs: list[list[dict]]) -> dict:
    from goat_model.metrics import bootstrap_ci, summarize

    measures = ("cer", "word_accuracy", "latency_ms")
    summary: dict = {}
    for metric in measures:
        run_level = [float(np.mean([rec[metric] for rec in run])) for run in runs]
        mean, std = summarize(run_level)
        summary[metric] = {"mean": mean, "std": std, "ci95": bootstrap_ci(run_level)}
    summary["n_images"] = len(runs[0]) if runs else 0
    summary["n_runs"] = len(runs)
    return summary
