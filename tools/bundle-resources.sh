#!/usr/bin/env bash
# Stage gitignored runtime models into src-tauri/models for the `resources`
# bundle key, which only accepts paths inside src-tauri. Runs as
# beforeBundleCommand (cwd = repo root). Skips when already staged.
set -euo pipefail

NAME="nllb-200-distilled-1.3B-ct2-int8"
SRC="models/${NAME}"
DST="src-tauri/models/${NAME}"

if [ -f "${DST}/model.bin" ]; then
    exit 0
fi
if [ ! -f "${SRC}/model.bin" ]; then
    echo "error: ${SRC}/model.bin missing - fetch it first (see release.yml Fetch NLLB model)" >&2
    exit 1
fi
mkdir -p "src-tauri/models"
cp -r "${SRC}" "${DST}"
