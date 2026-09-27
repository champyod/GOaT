"""Hybrid detector+recognizer: PP-OCRv5 boxes in, ThaiTrOCR lines out.

Heavy frameworks (paddle/torch) are stubbed: the hybrid takes its detector
and recognizer as constructor args, so unit tests use fakes and the real
wiring is proven on the GPU VM, not here.
"""

from __future__ import annotations

import sys
import types

import numpy as np


def _stub_heavy() -> None:
    import importlib.machinery

    torch = types.ModuleType("torch")
    torch.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
    sys.modules.setdefault("torch", torch)
    datasets = types.ModuleType("datasets")
    datasets.__spec__ = importlib.machinery.ModuleSpec("datasets", loader=None)
    datasets.Dataset = type("Dataset", (), {})
    sys.modules.setdefault("datasets", datasets)
    tf = types.ModuleType("transformers")
    tf.__spec__ = importlib.machinery.ModuleSpec("transformers", loader=None)
    for name in (
        "EarlyStoppingCallback",
        "Seq2SeqTrainer",
        "Seq2SeqTrainingArguments",
        "TrOCRProcessor",
        "VisionEncoderDecoderModel",
    ):
        setattr(tf, name, type(name, (), {}))
    sys.modules.setdefault("transformers", tf)


_stub_heavy()

from goat_model.ocr.engine import HybridLineOCR, OCRResult


class FakeDetector:
    def __init__(self, boxes: list[tuple[int, int, int, int]]) -> None:
        self._boxes = boxes

    def detect(self, image: np.ndarray) -> list[tuple[int, int, int, int]]:
        return list(self._boxes)


class FakeRecognizer:
    """One line per crop; identity-check spots the whole-image fallback."""

    def __init__(self) -> None:
        self.crops: list[tuple[int, int]] = []
        self.whole = 0
        self.page: np.ndarray | None = None

    def recognize(self, image: np.ndarray) -> OCRResult:
        if self.page is not None and image is self.page:
            self.whole += 1
            return OCRResult(text="whole-page", latency_ms=1.0)
        self.crops.append((image.shape[0], image.shape[1]))
        return OCRResult(text=f"line{len(self.crops)}", latency_ms=1.0)


def _page() -> np.ndarray:
    return np.zeros((60, 80, 3), dtype=np.uint8)


def _hybrid(
    boxes: list[tuple[int, int, int, int]],
) -> tuple[HybridLineOCR, FakeRecognizer, np.ndarray]:
    page = _page()
    rec = FakeRecognizer()
    rec.page = page
    # device="cpu": resolve_device("cuda") probes real torch; the load paths
    # that care about device are never taken with injected fakes.
    return HybridLineOCR(detector=FakeDetector(boxes), recognizer=rec, device="cpu"), rec, page


def test_lines_joined_top_to_bottom_despite_shuffled_boxes() -> None:
    backend, _, page = _hybrid([(0, 40, 79, 59), (0, 0, 79, 19)])
    out = backend.recognize(page)
    assert out.text == "line1\nline2"


def test_padding_and_clipping_stay_in_bounds() -> None:
    backend, rec, page = _hybrid([(-50, -50, 500, 500)])
    backend.recognize(page)
    assert rec.crops == [(60, 80)]


def test_no_boxes_falls_back_to_whole_image() -> None:
    backend, rec, page = _hybrid([])
    out = backend.recognize(page)
    assert out.text == "whole-page" and rec.whole == 1 and not rec.crops


def test_degenerate_box_skipped() -> None:
    backend, rec, page = _hybrid([(500, 500, 600, 600), (0, 30, 79, 49)])
    out = backend.recognize(page)
    assert out.text == "line1" and len(rec.crops) == 1


def test_hybrid_registered_for_selection() -> None:
    from goat_model.constants import OCR_IMG_SIZE, OCR_MODELS
    from goat_model.ocr.engine import BACKENDS

    assert HybridLineOCR.name in OCR_MODELS
    assert HybridLineOCR.name in OCR_IMG_SIZE
    assert BACKENDS[HybridLineOCR.name] is HybridLineOCR
