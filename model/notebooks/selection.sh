#!/bin/bash
set -e
# GOaT selection — shell twin of notebooks/selection.ipynb (same stem, same steps).
# Each step is one pure python call; Drive paths passed as args, same as the ipynb Args cell.
# Usage:
#   bash notebooks/selection.sh [DRIVE_ROOT]
#   nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT > /tmp/goat_log.txt 2>&1 &
# Defaults to Drive; pass a local path for local runs.

PROJECT="/content/GOaT/model"
DRIVE="${1:-/content/drive/MyDrive/GOaT}"
cd "$PROJECT"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
fi

uv sync --extra ocr --extra mt --extra train

uv run python notebooks/data/download_data.py --dataset scb-mt --out-dir "$DRIVE/datasets/mt"
uv run python notebooks/data/download_data.py --dataset flores200 --out-dir "$DRIVE/datasets/mt/test"
uv run python notebooks/data/download_data.py --dataset thaiocrbench --out-dir "$DRIVE/datasets/ocr/thaiocrbench"
uv run python notebooks/data/download_data.py --dataset thai-ocr-evaluation --out-dir "$DRIVE/datasets/ocr/thai-ocr-evaluation"
uv run python notebooks/selection/select_mt.py --mt-test-dir "$DRIVE/datasets/mt/test" --output "$DRIVE/results/mt_selection.json" --repeats 5 --seed 42
uv run python notebooks/selection/select_ocr.py --ocr-eval-dir "$DRIVE/datasets/ocr" --output "$DRIVE/results/ocr_selection.json" --repeats 5 --seed 42
uv run python notebooks/training/train_mt.py --mt-dir "$DRIVE/datasets/mt" --selection "$DRIVE/results/mt_selection.json" --output "$DRIVE/results/mt_training.json" --out-root /content/artifacts/mt_lora --seed 42
uv run python notebooks/training/train_ocr.py --selection "$DRIVE/results/ocr_selection.json" --data-root "$DRIVE/data" --output "$DRIVE/results/ocr_training.json" --out-root /content/artifacts/ocr --seed 42

echo "Done"
ls -lh "$DRIVE/results/" 2>&1
