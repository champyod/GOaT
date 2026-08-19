#!/usr/bin/env python3
"""Combine synthetic + real OCR images and do a stratified 70/15/15 split.

Stratification axis is the SOURCE (synthetic vs real) so both appear in every
split. Writes data/train, data/val, data/test (same image+gt layout as the OCR
eval dirs) plus a split manifest at data/split_manifest.json.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.utils import setup_seed, write_json


def _assets(root: Path) -> list[tuple[Path, Path]]:
    result: list[tuple[Path, Path]] = []
    for img in sorted(root.glob("*.png")) + sorted(root.glob("*.jpg")):
        gt = root / f"{img.stem}.txt"
        if gt.is_file():
            result.append((img, gt))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Stratified 70/15/15 OCR split.")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    setup_seed(args.seed)
    rng = random.Random(args.seed)

    synthetic = _assets(c.SYNTHETIC)
    real = _assets(c.REAL)
    if not synthetic:
        raise SystemExit(
            f"no synthetic images+gt in {c.SYNTHETIC} (see scripts/generate_synthetic.py)"
        )
    if not real:
        raise SystemExit(f"no real images+gt in {c.REAL}")

    print(f"found {len(synthetic)} synthetic, {len(real)} real images")

    def split_group(
        group: list[tuple[Path, Path]], prefix: str
    ) -> dict[str, list[tuple[Path, Path]]]:
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
        name: c.DATA / name.split("_")[1]
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

    write_json(
        c.DATA / "split_manifest.json", {"seed": args.seed, "counts": counts, "files": manifest}
    )

    total = {}
    for split in ("train", "val", "test"):
        syn = counts.get(f"syn_{split}", 0)
        real = counts.get(f"real_{split}", 0)
        total[split] = syn + real
        print(f"  {split}: {total[split]} ({syn} syn, {real} real)")
    print(f"expected: {c.DATA_PLAN['train']}/{c.DATA_PLAN['val']}/{c.DATA_PLAN['test']}")


if __name__ == "__main__":
    main()
