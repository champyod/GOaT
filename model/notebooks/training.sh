#!/bin/bash
set -e
# GOaT training — shell twin of notebooks/training.ipynb (same stem, same steps).
# Each step is one pure python call; Drive paths passed as args, same as the ipynb Args cell.
# Usage:
#   bash notebooks/training.sh [DRIVE_ROOT]
#   nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT > /tmp/goat_training_log.txt 2>&1 &
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
ART_MT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_MT; print(ART_MT)')"
ART_OCR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_OCR; print(ART_OCR)')"
MT_DATA="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["mt"])')"
RESULTS="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["results"])')"
DATA_ROOT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["data_root"])')"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
fi

uv sync --extra ocr --extra mt --extra train

# Full opencv-python (via synthtiger) needs system libGL; install only when cv2 fails to import.
PYTHONPATH=src uv run python -c "import cv2" 2>/dev/null || (apt-get update -qq && apt-get install -y -q libgl1 libglib2.0-0)

uv run python notebooks/training/train_mt.py --mt-dir "$MT_DATA" --selection "$RESULTS/mt_selection.json" --output "$RESULTS/mt_training.json" --out-root "$ART_MT" --seed "$SEED"
uv run python scripts/generate_synthetic.py --out "$DATA_ROOT/synthetic"
uv run python notebooks/training/train_ocr.py --selection "$RESULTS/ocr_selection.json" --data-root "$DATA_ROOT" --output "$RESULTS/ocr_training.json" --out-root "$ART_OCR" --seed "$SEED"

echo "Done"
ls -lh "$RESULTS/" 2>&1
