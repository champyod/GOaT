#!/bin/bash
set -e
# GOaT training — shell twin of notebooks/training.ipynb (same stem, same steps).
# Each step is one pure python call; Drive paths passed as args, same as the ipynb Args cell.
# Usage:
#   bash notebooks/training.sh [DRIVE_ROOT] [--debug]
#   nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_training_log.txt 2>&1 &
# Defaults to Drive; pass a local path for local runs.

PROJECT="/content/GOaT/model"
DEBUG_ARGS=""
DRIVE=""
for arg in "$@"; do
  case "$arg" in
    --debug) DEBUG_ARGS="--debug" ;;
    -*) echo "Unknown flag: $arg" >&2; exit 2 ;;
    *) if [ -z "$DRIVE" ]; then DRIVE="$arg"; fi ;;
  esac
done
DRIVE="${DRIVE:-/content/drive/MyDrive/GOaT}"
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

# Witness for vanishing jobs: every python dumps a traceback on fatal signals,
# and the shell logs signal/exit receipt with timestamps (preemption/OOM leaves a mark).
export PYTHONFAULTHANDLER=1
trap 'echo "[trap] training.sh got TERM/INT at $(date -u +%FT%TZ)" >&2; exit 143' TERM INT
trap 'code=$?; echo "[trap] training.sh exiting code=$code at $(date -u +%FT%TZ)" >&2' EXIT

# Install (dual path, same set): primary `colab install -s goat -r requirements.txt`
# from laptop once; fallback below (uv sync) still runs so sh works when skipped.
# Mirrors ipynb %pip cell — both install the same extras.
SEED="$(PYTHONPATH=src python3 -c 'from goat_model.constants import SEED; print(SEED)')"
ART_MT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_MT; print(ART_MT)')"
ART_OCR="$(PYTHONPATH=src python3 -c 'from goat_model.constants import ART_OCR; print(ART_OCR)')"
MT_DATA="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["mt"])')"
RESULTS="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["results"])')"
DATA_ROOT="$(PYTHONPATH=src python3 -c 'from goat_model.constants import DRIVE_PATHS; print(DRIVE_PATHS["data_root"])')"
# Synth data stays fully local: HF is the persistent source, and 10k-file
# Drive traffic contends with log/result syncing. Only small result JSONs
# ($RESULTS) ever touch Drive in this script.
LOCAL_SYNTH="/tmp/goat_synth_data"
mkdir -p "$LOCAL_SYNTH"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
fi

# uv prints per-package lines only when done; heartbeat the silent multi-GB install.
SYNC_T0=$(date +%s)
( while true; do echo "[sync] installing ... .venv $(du -sh .venv 2>/dev/null | cut -f1) elapsed=$(( $(date +%s) - SYNC_T0 ))s" >&2; sleep 30; done ) &
HEART_PID=$!
uv sync --extra ocr --extra mt --extra train
kill $HEART_PID 2>/dev/null
wait $HEART_PID 2>/dev/null || true

# Full opencv-python (via synthtiger) needs system libGL; install only when cv2 fails to import.
PYTHONPATH=src uv run python -c "import cv2" $DEBUG_ARGS 2>/dev/null || (apt-get update -qq && apt-get install -y -q libgl1 libglib2.0-0)

uv run python notebooks/training/train_mt.py --mt-dir "$MT_DATA" --selection "$RESULTS/mt_selection.json" --output "$RESULTS/mt_training.json" --out-root "$ART_MT" --seed "$SEED" $DEBUG_ARGS
uv run python scripts/generate_synthetic.py --out "$LOCAL_SYNTH/synthetic" --real "$DATA_ROOT/real" $DEBUG_ARGS
uv run python notebooks/training/train_ocr.py --selection "$RESULTS/ocr_selection.json" --data-root "$LOCAL_SYNTH" --output "$RESULTS/ocr_training.json" --out-root "$ART_OCR" --seed "$SEED" $DEBUG_ARGS

echo "Done"
ls -lh "$RESULTS/" 2>&1
