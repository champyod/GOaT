#!/usr/bin/env python3
"""Keep a CLI session bound to its live VM.

Two modes (both use the CLI's own state store + server truth):

- refresh (default): rewrite the stored tunnel token/url from a fresh
  ``list_assignments()`` response. Run on a loop (~45 min, under the
  ~60 min token TTL) and the 404/401 prune from issue #106 never triggers.
- adopt: rebind a pruned name to a live ``[?]`` orphan endpoint.

Usage:
    rebind.py NAME                  # one-shot refresh
    rebind.py NAME --loop           # refresh every 45 min forever
    rebind.py NAME --endpoint EPT   # adopt orphan (post-prune recovery)
    rebind.py NAME --loop --interval 2700
"""
from __future__ import annotations

import argparse
import sys
import time

# Run with the CLI venv python so colab_cli resolves:
#   ~/.local/share/uv/tools/google-colab-cli/bin/python rebind.py NAME
from colab_cli.common import state as _cli_state  # noqa: E402
from colab_cli.state import SessionState  # noqa: E402


def _fresh_assignment(endpoint: str | None, name: str):
    _, assignments = _cli_state.sync_sessions()
    if endpoint:
        for a in assignments:
            if a.endpoint == endpoint:
                return a
        raise SystemExit(f"endpoint {endpoint} not on server; check `colab sessions`")
    stored = _cli_state.store.get(name)
    if stored is None:
        raise SystemExit(f"no local entry {name!r}; pass --endpoint to adopt")
    for a in assignments:
        if a.endpoint == stored.endpoint:
            return a
    raise SystemExit(f"{name}ᴹs endpoint gone from server; VM is dead, re-provision")


def refresh(name: str, endpoint: str | None) -> SessionState:
    a = _fresh_assignment(endpoint, name)
    entry = SessionState(
        name=name,
        token=a.runtime_proxy_info.token,
        url=a.runtime_proxy_info.url,
        endpoint=a.endpoint,
        variant=a.variant.name,
        accelerator=a.accelerator.value,
    )
    _cli_state.store.add(entry)
    print(f"bound {name} -> {a.endpoint} (fresh token)", flush=True)
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh or adopt a CLI session binding.")
    parser.add_argument("name")
    parser.add_argument("--endpoint", default=None, help="adopt this orphan endpoint")
    parser.add_argument("--loop", action="store_true", help="refresh forever")
    parser.add_argument("--interval", type=float, default=2700.0, help="seconds between refreshes")
    args = parser.parse_args()
    refresh(args.name, args.endpoint)
    while args.loop:
        time.sleep(args.interval)
        try:
            refresh(args.name, None)
        except SystemExit as err:
            print(f"rebind: {err}", flush=True)


if __name__ == "__main__":
    main()
