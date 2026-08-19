"""Minimal CLI used by `uv run main.py` (CI smoke test) and `goat-model`."""

from __future__ import annotations

import argparse
import sys


def _hello() -> int:
    print("Hello from model!")
    print("GOaT model pipeline scaffold (see model/README.md)")
    print("Phases: data -> select -> train -> export -> eval")
    return 0


def _plan() -> int:
    from goat_model import constants as c

    print("OCR:", dict(c.OCR_IMG_SIZE))
    print("MT:", c.MT_MODELS, "lang codes:", c.LANG_CODES)
    print("Split plan:", c.DATA_PLAN)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goat-model", description="GOaT model pipeline")
    parser.add_argument("command", nargs="?", default="hello", help="hello | plan")
    args = parser.parse_args(argv)

    if args.command == "plan":
        return _plan()
    return _hello()


if __name__ == "__main__":
    sys.exit(main())
