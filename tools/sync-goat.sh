#!/bin/bash
# sync-goat: pull a VM training log to this Pi for tools/host_watchdog.py.
# - Appends only the tail each poll; touches a sync heartbeat file on success
#   so the watchdog can tell "VM quiet" apart from "sync stalled".
# - Defaults match the watchdog: out defaults to ~/synced/goat.log,
#   sync-file defaults to <out-dir>/.sync_ok.
# Usage: bash tools/sync-goat.sh --vm-log /tmp/goat_training_log.txt [--session goat]
#        [--out ~/synced/goat.log] [--sync-file ~/synced/.sync_ok]
#        [--interval 10]
set -u
SESSION="goat"
VM_LOG=""
OUT="$HOME/synced/goat.log"
SYNC_FILE=""
INTERVAL=10
while [ $# -gt 0 ]; do
    case "$1" in
        -s|--session) SESSION="$2"; shift 2;;
        --vm-log) VM_LOG="$2"; shift 2;;
        -o|--out) OUT="$2"; shift 2;;
        --sync-file) SYNC_FILE="$2"; shift 2;;
        --interval) INTERVAL="$2"; shift 2;;
        -h|--help) sed -n '2,9p' "$0"; exit 0;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done
[ -z "$SYNC_FILE" ] && SYNC_FILE="$(dirname "$OUT")/.sync_ok"
if [ -z "$VM_LOG" ]; then echo "missing required --vm-log PATH" >&2; exit 2; fi
mkdir -p "$(dirname "$OUT")"
touch "$OUT"
while true; do
    TS=$(date '+%H:%M:%S')
    CHUNK=$(colab exec -s "$SESSION" <<PY 2>/tmp/sync-goat.err
import os
try:
    with open("$VM_LOG", "r", errors="replace") as f:
        print(f.read()[-8000:], end="")
except Exception as e:
    print(f"[sync-miss] {e}")
PY
)
    CLEAN=$(printf '%s' "$CHUNK" | grep -v '^\[colab\]' | tail -c 8000)
    if printf '%s' "$CHUNK" | grep -q '^\[sync-miss\]'; then
        echo "[$TS] fetch failed: $(printf '%s' "$CHUNK" | head -c 300)" >&2
    elif [ -n "$CLEAN" ]; then
        printf '%s\n' "$CLEAN" >> "$OUT"
        touch "$SYNC_FILE"
        echo "[$TS] synced ${#CLEAN}B total=$(wc -c < "$OUT")B -> $OUT"
    else
        echo "[$TS] miss: $(head -c 300 /tmp/sync-goat.err)"
    fi
    sleep "$INTERVAL"
done
