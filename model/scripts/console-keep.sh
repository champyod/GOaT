#!/bin/bash
# console-keep: reconnect `colab console` instantly when the transport drops.
# - Refreshes the session binding first (dodges the #106 token-expiry prune:
#   a stale token looks exactly like a dead session).
# - Exits (no spin) when the VM is truly gone from the server.
# - Backs off on rapid failures; resets after a healthy connection.
# Usage: bash model/scripts/console-keep.sh [-s NAME]
set -u
NAME="${2:-goat}"
CLI_PY="$HOME/.local/share/uv/tools/google-colab-cli/bin/python"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
backoff=2
while true; do
    "$CLI_PY" "$SCRIPT_DIR/rebind.py" "$NAME" >/dev/null 2>&1
    start=$(date +%s)
    colab console -s "$NAME"
    code=$?
    lived=$(( $(date +%s) - start ))
    if ! colab sessions 2>/dev/null | grep -q "$NAME\|\[?\]"; then
        echo "[console-keep] no assignment on server - VM is gone, re-provision (not retrying)"
        exit 1
    fi
    if [ "$lived" -ge 60 ]; then
        backoff=2
    else
        backoff=$(( backoff < 30 ? backoff * 2 : 30 ))
    fi
    echo "[console-keep] console ended (code=$code, lived=${lived}s) - reconnect in ${backoff}s (Ctrl+C to stop)"
    sleep "$backoff"
done
