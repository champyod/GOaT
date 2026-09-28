"""Tesseract fallback backend: Thai space-join + CLI runner.

The real `tesseract` binary is absent here, so the backend takes an injected
`runner` callable; the missing-binary guard is pinned by making the fake
raise FileNotFoundError exactly like subprocess does.
"""

from __future__ import annotations

import sys
import types

import numpy as np


def _stub_heavy() -> None:
    import importlib.machinery

    for name in ("torch", "transformers"):
        mod = types.ModuleType(name)
        mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        sys.modules.setdefault(name, mod)
    datasets = types.ModuleType("datasets")
    datasets.__spec__ = importlib.machinery.ModuleSpec("datasets", loader=None)
    datasets.Dataset = type("Dataset", (), {})
    sys.modules.setdefault("datasets", datasets)
    tf = sys.modules["transformers"]
    for name in (
        "EarlyStoppingCallback",
        "Seq2SeqTrainer",
        "Seq2SeqTrainingArguments",
        "TrOCRProcessor",
        "VisionEncoderDecoderModel",
    ):
        setattr(tf, name, type(name, (), {}))


_stub_heavy()

from goat_model.ocr.engine import TesseractOCR, join_thai_spaces


def test_join_thai_spaces_glues_thai_only() -> None:
    assert join_thai_spaces("ส วัส ดี") == "สวัสดี"
    assert join_thai_spaces("a b") == "a b"
    assert join_thai_spaces("ก a") == "ก a"
    assert join_thai_spaces("a ก") == "a ก"
    assert join_thai_spaces("( ด )") == "( ด )"
    assert join_thai_spaces("") == ""


def test_backend_strips_and_joins_runner_output() -> None:
    backend = TesseractOCR(runner=lambda path: "  ส วัส ดี  \nOfficial site  \n")
    out = backend.recognize(np.zeros((20, 30, 3), dtype=np.uint8))
    assert out.text == "สวัสดี\nOfficial site"
    assert out.latency_ms >= 0.0


def test_missing_binary_fails_loudly() -> None:
    import pytest

    def no_binary(path: str) -> str:
        raise FileNotFoundError("tesseract")

    backend = TesseractOCR(runner=no_binary)
    with pytest.raises(RuntimeError, match="apt install tesseract-ocr"):
        backend.recognize(np.zeros((20, 30, 3), dtype=np.uint8))


def test_tesseract_registered_for_selection() -> None:
    from goat_model.constants import OCR_IMG_SIZE, OCR_MODELS
    from goat_model.ocr.engine import BACKENDS

    assert TesseractOCR.name in OCR_MODELS
    assert TesseractOCR.name in BACKENDS
    assert BACKENDS[TesseractOCR.name] is TesseractOCR
    assert OCR_IMG_SIZE[TesseractOCR.name] is None  # full pages, no resize
