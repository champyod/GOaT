#!/usr/bin/env bash
# vm_watch.sh — in-VM run watcher. Tails a worker log with plain tail/curl
# (no Jupyter kernels involved) and posts to Discord on errors, silence,
# and completion.
#
#   bash tools/vm_watch.sh --log /content/worker_a.log --tag goat-A \
#       --done /content/worker_a.DONE [--webhook-file /content/.discord_wh] \
#       [--interval 300] [--silence 1800]
#
# The webhook file holds one line: the Discord webhook URL. It is a secret:
# transferred to the VM directly, never committed (see .gitignore).
set -u
LOG=""
TAG=""
DONE=""
WEBHOOK_FILE="/content/.discord_wh"
INTERVAL=300
SILENCE=1800
usage() { echo "usage: vm_watch.sh --log FILE --tag NAME --done FILE [--webhook-file F] [--interval S] [--silence S]" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --log) LOG=$2; shift 2 ;;
    --tag) TAG=$2; shift 2 ;;
    --done) DONE=$2; shift 2 ;;
    --webhook-file) WEBHOOK_FILE=$2; shift 2 ;;
    --interval) INTERVAL=$2; shift 2 ;;
    --silence) SILENCE=$2; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$LOG" ] && [ -n "$TAG" ] && [ -n "$DONE" ] || usage
[ -f "$WEBHOOK_FILE" ] || { echo "vm_watch: webhook file missing: $WEBHOOK_FILE" >&2; exit 1; }
WH=$(cat "$WEBHOOK_FILE")
post() { curl -s -m 20 -X POST -H 'Content-Type: application/json' -d "{\"content\": \"[$TAG] $1\"}" "$WH" > /dev/null 2>&1 || true; }
post "watcher online, monitoring $(basename "$LOG")"
last_line=$(wc -l < "$LOG" 2>/dev/null || echo 0)
last_change=$(date +%s)
err_sent=0
while true; do
  sleep "$INTERVAL"
  [ -f "$DONE" ] && post "DONE — run finished" && exit 0
  total=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  if [ "$total" -gt "$last_line" ]; then
    if sed -n "$((last_line + 1)),\$p" "$LOG" | grep -qiE "traceback|ERROR .*error|failed|exit=[1-9]|OutOfMemory|CUDA out of memory|killed"; then
      if [ "$err_sent" = "0" ]; then post "error pattern in log (see $LOG)"; err_sent=1; fi
    fi
    last_line=$total
    last_change=$(date +%s)
  fi
  if [ $(( $(date +%s) - last_change )) -gt "$SILENCE" ]; then
    post "silent $((SILENCE / 60))min, still alive"
    last_change=$(date +%s)
  fi
done
