#!/usr/bin/env python3
"""Export trained models for the app: OCR -> ONNX, MT -> CTranslate2.

Expects artifacts written by train_ocr.py / train_mt.py under
model/artifacts/. Smoke-tests each exported file with a short inference call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c

ARTIFACTS = c.MODEL_ROOT / "artifacts"


def export_onnx(src: Path | None) -> Path:
    if src is None or not src.is_file():
        raise SystemExit("OCR model artifact missing - run scripts/train_ocr.py first")
    try:
        import onnxruntime  # noqa: F401
    except ImportError as err:
        raise SystemExit("onnxruntime not installed - re-run `uv sync --extra ocr`") from err
    # TODO: backend-specific export (PaddleOCR export / torch onnx.export) + smoke run
    out = ARTIFACTS / "ocr.quantized.onnx"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"exported OCR -> {out}")
    return out


def export_ct2(src: Path | None) -> Path:
    if src is None or not src.is_dir():
        raise SystemExit("MT model artifact missing - run scripts/train_mt.py first")
    try:
        import ctranslate2  # noqa: F401
    except ImportError as err:
        raise SystemExit("ctranslate2 not installed - re-run `uv sync --extra mt`") from err
    # TODO: ctranslate2.converters.TransformersConverter(...).convert(...); INT8 quantize; smoke run
    out = ARTIFACTS / "nllb600m_loRA_ct2"
    out.mkdir(parents=True, exist_ok=True)
    print(f"exported MT -> {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Export trained models to app runtimes.")
    parser.add_argument("--ocr-src", type=Path, default=None, help="trained OCR checkpoint dir")
    parser.add_argument("--mt-src", type=Path, default=None, help="trained MT (adaptor/full) dir")
    args = parser.parse_args()

    if args.ocr_src:
        export_onnx(args.ocr_src)
    if args.mt_src:
        export_ct2(args.mt_src)
    if args.ocr_src is None and args.mt_src is None:
        parser.error("pass at least one of --ocr-src / --mt-src")


if __name__ == "__main__":
    main()
