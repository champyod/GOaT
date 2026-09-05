#!/usr/bin/env python3
"""Keep a CLI session bound to its live VM.

Two modes (both use the CLI's own state store + server truth):

- refresh (default): rewrite the stored tunnel token/url from a fresh
  ``list_assignments()`` response. Run on a loop (~45 min, under the
  ~60 min token TTL) and the 404/401 prune from issue #106 never triggers.
- adopt: rebind a pruned name to a live ``[?]`` orphan endpoint.

Usage:
    rebind.py NAME                  # selector (current first, Enter keeps it)
    rebind.py NAME --no-select      # silent refresh, no prompt
    rebind.py NAME --loop           # refresh every 45 min forever
    rebind.py NAME --endpoint EPT   # adopt orphan directly
    rebind.py NAME --pick N         # non-interactive index
"""
from __future__ import annotations

import argparse
import time

# Run with the CLI venv python so colab_cli resolves:
#   ~/.local/share/uv/tools/google-colab-cli/bin/python rebind.py NAME
from colab_cli.common import state as _cli_state  # noqa: E402
from colab_cli.state import SessionState  # noqa: E402


def _orphans(assignments, bound: set[str]) -> list:
    return [a for a in assignments if a.endpoint not in bound]


def _pick_orphan(orphans: list, pick: int | None):
    for i, a in enumerate(orphans):
        print(f"  [{i}] {a.endpoint} ({a.accelerator} / {a.variant})", flush=True)
    if len(orphans) == 1 and pick is None:
        print("one orphan - adopting it", flush=True)
        return orphans[0]
    if pick is not None:
        if 0 <= pick < len(orphans):
            return orphans[pick]
        raise SystemExit(f"--pick {pick} out of range 0..{len(orphans) - 1}")
    raw = input(f"pick orphan [0..{len(orphans) - 1}]: ").strip()
    if not raw.isdigit() or not 0 <= int(raw) < len(orphans):
        raise SystemExit("no selection made")
    return orphans[int(raw)]


def _fresh_assignment(endpoint: str | None, name: str, pick: int | None):
    _, assignments = _cli_state.sync_sessions()
    if endpoint:
        for a in assignments:
            if a.endpoint == endpoint:
                return a
        raise SystemExit(f"endpoint {endpoint} not on server; check `colab sessions`")
    stored = _cli_state.store.get(name)
    if stored is not None:
        for a in assignments:
            if a.endpoint == stored.endpoint:
                return a
        raise SystemExit(f"{name}'s endpoint gone from server; VM is dead, re-provision")
    bound = {s.endpoint for s in _cli_state.store.list().values()}
    orphans = _orphans(assignments, bound)
    if not orphans:
        raise SystemExit("no orphan on server; VM is dead, re-provision")
    return _pick_orphan(orphans, pick)


def refresh(name: str, endpoint: str | None, pick: int | None = None) -> SessionState:
    a = _fresh_assignment(endpoint, name, pick)
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


def watch(name: str, interval: float, force: bool) -> None:
    """Force-select a session, hold the binding until it vanishes, then
    re-show the selector. Sessions data refreshes every ``interval``."""
    import datetime

    bound: str | None = None
    picked_at = 0.0
    first = True
    try:
        while True:
            _, assignments = _cli_state.sync_sessions()
            alive = bound is not None and any(a.endpoint == bound for a in assignments)
            if alive:
                age = time.monotonic() - picked_at
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {name} -> {bound} alive ({age:.0f}s held)", flush=True)
            else:
                if bound is not None:
                    print(f"{name} -> {bound} gone from server", flush=True)
                bound = None
                stored = None if (first and force) else _cli_state.store.get(name)
                if stored is not None:
                    for a in assignments:
                        if a.endpoint == stored.endpoint:
                            bound = stored.endpoint
                            picked_at = time.monotonic()
                            print(f"keeping existing binding {name} -> {bound}", flush=True)
                            break
                if bound is None:
                    bound_endpoints = {s.endpoint for s in _cli_state.store.list().values()}
                    orphans = _orphans(assignments, bound_endpoints)
                    if not orphans:
                        print("no orphan on server - waiting", flush=True)
                    else:
                        picked = _pick_orphan(orphans, None)
                        entry = refresh(name, picked.endpoint, None)
                        bound = entry.endpoint
                        picked_at = time.monotonic()
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        print("watch stopped", flush=True)


def select(name: str, pick: int | None) -> SessionState:
    """Selector by default: current binding first, then orphans.
    Empty Enter keeps [0]. Returns the saved entry."""
    _, assignments = _cli_state.sync_sessions()
    cands: list[tuple[str, object]] = []
    stored = _cli_state.store.get(name)
    if stored is not None:
        live = next((a for a in assignments if a.endpoint == stored.endpoint), None)
        if live is not None:
            cands.append(("current", live))
    bound = {s.endpoint for s in _cli_state.store.list().values()}
    cands.extend(("orphan", a) for a in _orphans(assignments, bound))
    if not cands:
        raise SystemExit("nothing on server; VM is dead, re-provision")
    for i, (tag, a) in enumerate(cands):
        print(f"  [{i}] {tag} {a.endpoint} ({a.accelerator} / {a.variant})", flush=True)
    if pick is not None:
        if 0 <= pick < len(cands):
            chosen = cands[pick][1]
        else:
            raise SystemExit(f"--pick {pick} out of range 0..{len(cands) - 1}")
    elif len(cands) == 1:
        print("one candidate - keeping it", flush=True)
        chosen = cands[0][1]
    else:
        raw = input(f"pick [0..{len(cands) - 1}] (Enter keeps [0]): ").strip()
        if raw == "":
            chosen = cands[0][1]
        elif not raw.isdigit() or not 0 <= int(raw) < len(cands):
            raise SystemExit("no selection made")
        else:
            chosen = cands[int(raw)][1]
    entry = SessionState(
        name=name,
        token=chosen.runtime_proxy_info.token,
        url=chosen.runtime_proxy_info.url,
        endpoint=chosen.endpoint,
        variant=chosen.variant.name,
        accelerator=chosen.accelerator.value,
    )
    _cli_state.store.add(entry)
    print(f"bound {name} -> {chosen.endpoint} (fresh token)", flush=True)
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh or adopt a CLI session binding.")
    parser.add_argument("name")
    parser.add_argument("--endpoint", default=None, help="adopt this orphan endpoint")
    parser.add_argument("--pick", type=int, default=None, help="index when several exist")
    parser.add_argument("--no-select", action="store_true", help="skip selector, silent refresh")
    parser.add_argument("--loop", action="store_true", help="refresh forever")
    parser.add_argument("--watch", action="store_true", help="force-select, hold, re-show selector")
    parser.add_argument("--force", action="store_true", help="with --watch: select even if bound")
    parser.add_argument("--interval", type=float, default=None, help="seconds between refreshes")
    args = parser.parse_args()
    if args.watch:
        watch(args.name, args.interval if args.interval is not None else 5.0, args.force)
        return
    if not args.no_select and args.endpoint is None:
        select(args.name, args.pick)
        while args.loop:
            time.sleep(args.interval if args.interval is not None else 2700.0)
            try:
                refresh(args.name, None, None)
            except SystemExit as err:
                print(f"rebind: {err}", flush=True)
        return
    refresh(args.name, args.endpoint, args.pick)
    while args.loop:
        time.sleep(args.interval if args.interval is not None else 2700.0)
        try:
            refresh(args.name, None, None)
        except SystemExit as err:
            print(f"rebind: {err}", flush=True)


if __name__ == "__main__":
    main()
