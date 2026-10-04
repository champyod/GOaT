#!/usr/bin/env bash
# Stage gitignored runtime models into src-tauri/models for the `resources`
# bundle key, which only accepts paths inside src-tauri. Runs as
# beforeBundleCommand (cwd = repo root). Skips when already staged.
set -euo pipefail

# Model-less builds set this: tauri.conf.json's `resources` is emptied through
# TAURI_CONFIG in that configuration, so nothing is bundled and there is no
# staging for the bundler to consume. Unset keeps the local path unchanged.
if [ -n "${GOAT_SKIP_MODEL_STAGE:-}" ]; then
    echo "skip: GOAT_SKIP_MODEL_STAGE set - not staging runtime models"
    exit 0
fi

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
