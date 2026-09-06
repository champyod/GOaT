"""Minimal CLI used by `uv run main.py` (CI smoke test) and `goat-model`."""

from __future__ import annotations

import argparse
import sys

from goat_model.utils import log_call


from goat_model.log import info as _info


@log_call
def _hello() -> int:
    _info("cli", "Hello from model!")
    _info("cli", "GOaT model pipeline scaffold (see model/README.md)")
    _info("cli", "Phases: data -> select -> train -> export -> eval")
    return 0


@log_call
def _plan() -> int:
    from goat_model import constants as c

    _info("cli", "plan", ocr=dict(c.OCR_IMG_SIZE))
    _info("cli", "plan", mt=c.MT_MODELS, langs=c.LANG_CODES)
    _info("cli", "plan", split=c.DATA_PLAN)
    return 0


@log_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goat-model", description="GOaT model pipeline")
    parser.add_argument("command", nargs="?", default="hello", help="hello | plan")
    args = parser.parse_args(argv)

    if args.command == "plan":
        return _plan()
    return _hello()


if __name__ == "__main__":
    sys.exit(main())
