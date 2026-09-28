#!/bin/bash
# Fresh/recycled-VM bootstrap: clone-or-pull the repo, then launch the full
# stack (training + log sync + kernel janitor). Everything persistent lives
# on Drive (checkpoints, partial results, HF cache), so a wiped VM resumes
# where it died. Needs Drive mounted first (browser OAuth - not scriptable).
#
# Usage on a fresh VM over SSH (one paste):
#   bash <(curl -LsSf https://raw.githubusercontent.com/champyod/GOaT/main/tools/vm_bootstrap.sh) /content/drive/MyDrive/GOaT
# Or from an existing checkout:
#   bash /content/GOaT/tools/vm_bootstrap.sh /content/drive/MyDrive/GOaT
set -e

DRIVE="${1:-/content/drive/MyDrive/GOaT}"
REPO_URL="https://github.com/champyod/GOaT.git"
DEST="/content/GOaT"

if [ ! -d "/content/drive/MyDrive" ]; then
  echo "Drive not mounted - mount it in the Colab UI first, then rerun." >&2
  exit 2
fi

if [ -d "$DEST/.git" ]; then
  echo "[bootstrap] pull $DEST"
  git -C "$DEST" pull --ff-only
else
  echo "[bootstrap] clone into $DEST"
  git clone "$REPO_URL" "$DEST"
fi

echo "[bootstrap] launch training"
nohup bash "$DEST/model/notebooks/training.sh" "$DRIVE" > /tmp/goat_training_log.txt 2>&1 &
echo "[bootstrap] launch log sync"
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
echo "[bootstrap] launch kernel janitor"
nohup python3 "$DEST/tools/vm_reaper.py" --interval 300 > /tmp/vm_reaper.log 2>&1 &
echo "[bootstrap] all running - watch with: tail -n 25 /tmp/goat_training_log.txt"
