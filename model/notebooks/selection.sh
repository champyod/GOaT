#!/bin/bash
set -e
# GOaT selection — shell twin of notebooks/selection.ipynb (steps 6-8:
# data download + MT/OCR selection), same order, same args.
# Training lives in notebooks/training.sh (twin of training.ipynb).
# Each step is one pure python call; Drive paths passed as args, same as the ipynb Args cell.
# Usage:
#   bash notebooks/selection.sh [DRIVE_ROOT]
#   nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT > /tmp/goat_log.txt 2>&1 &
# Defaults to Drive; pass a local path for local runs.

PROJECT="/content/GOaT/model"
DRIVE="${1:-/content/drive/MyDrive/GOaT}"
cd "$PROJECT"

# Model + dataset weights cache on Drive: first run downloads (needs
# HF_TOKEN for gated sets), reruns reuse with no re-download. Local SSD
# would be faster per-file, but persistence across VMs wins by GBs.
export HF_HOME="$DRIVE/hf_cache"
export HF_HUB_CACHE="$DRIVE/hf_cache"

# Kill third-party \r progress bars (datasets, Trainer) in batch runs:
# our LogProgress already prints newline heartbeats when stdout is not a TTY,
# so logs read correctly with plain `tail`, no `tr`/`grep` post-processing.
export TQDM_DISABLE=1

# Install (dual path, same set): primary `colab install -s goat -r requirements.txt`
# from laptop once; fallback below (uv sync) still runs so sh works when skipped.
# Mirrors ipynb %pip cell — both install the same extras.
SEED="$(PYTHONPATH=src python3 -c 'from goat_model.constants import SEED; print(SEED)')"
REPEATS_MT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import MT_N_RUNS; print(MT_N_RUNS)')"
REPEATS_OCR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import OCR_N_RUNS; print(OCR_N_RUNS)')"
ART_MT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_MT; print(ART_MT)')"
ART_OCR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_OCR; print(ART_OCR)')"
MT_DATA="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["mt"])')"
MT_TEST_DIR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["mt_test"])')"
OCR_EVAL_DIR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["ocr_eval"])')"
RESULTS="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["results"])')"
DATA_ROOT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["data_root"])')"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
fi

uv sync --extra ocr --extra mt --extra train

uv run python notebooks/data/download_data.py --dataset scb-mt --out-dir "$MT_DATA"
uv run python notebooks/data/download_data.py --dataset flores200 --out-dir "$MT_TEST_DIR"
uv run python notebooks/data/download_data.py --dataset thaiocrbench --out-dir "$OCR_EVAL_DIR/thaiocrbench"
uv run python notebooks/data/download_data.py --dataset thai-ocr-evaluation --out-dir "$OCR_EVAL_DIR/thai-ocr-evaluation"
uv run python notebooks/selection/select_mt.py --mt-test-dir "$MT_TEST_DIR" --output "$RESULTS/mt_selection.json" --repeats "$REPEATS_MT" --seed "$SEED"
uv run python notebooks/selection/select_ocr.py --ocr-eval-dir "$OCR_EVAL_DIR" --output "$RESULTS/ocr_selection.json" --repeats "$REPEATS_OCR" --seed "$SEED"

echo "Done"
ls -lh "$RESULTS/" 2>&1
