"""CPU OCR backends.

Heavy frameworks (paddle/onnxruntime/torch) are imported lazily so that
`uv sync` with only base deps can still run utils/metrics/smoke tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from goat_model.utils import have


@dataclass
class OCRResult:
    text: str
    latency_ms: float


class OCRBackend(Protocol):
    def recognize(self, image: np.ndarray) -> OCRResult: ...


class PaddleOCRv5(OCRBackend):
    """PaddleOCR PP-OCRv5-mobile (detection + recognition, CPU)."""

    name = "PP-OCRv5-mobile"

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self._engine = None

    def _load(self):
        if self._engine is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as err:
                raise RuntimeError("paddleocr not installed — run `uv sync --extra ocr`") from err
            # TODO: confirm kwargs against installed PaddleOCR v3.x API
            self._engine = PaddleOCR(
                ocr_version="PP-OCRv5", use_gpu=self.device != "cpu", lang="th"
            )
        return self._engine

    def recognize(self, image: np.ndarray) -> OCRResult:
        import time

        engine = self._load()
        start = time.perf_counter()
        result = engine.predict(image)
        latency = (time.perf_counter() - start) * 1000.0
        text = "\n".join(line[1][0] for page in result for line in page[1] if line[1][0])
        return OCRResult(text=text, latency_ms=latency)


class ThaiTrOCR(OCRBackend):
    """OpenThaiGPT ThaiTrOCR (Vision Transformer encoder + Electra decoder).

    Loader follows the official model card "How to Use":
    https://huggingface.co/openthaigpt/thai-trocr
    """

    name = "ThaiTrOCR"
    model_id = "openthaigpt/thai-trocr"
    img_size = 384

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self._model = None
        self._processor = None

    def _load(self):
        if self._model is None:
            if not have("torch", "transformers"):
                raise RuntimeError("torch/transformers not installed — run `uv sync --extra ocr`")
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel

            from goat_model.utils import setup_seed

            self._processor = TrOCRProcessor.from_pretrained(self.model_id)
            self._model = VisionEncoderDecoderModel.from_pretrained(self.model_id)
            self._model.eval()
            if self.device == "cpu":
                self._model = self._model.to("cpu")
            setup_seed(42)
        return self._model, self._processor

    def recognize(self, image: np.ndarray) -> OCRResult:
        import time

        import torch
        from PIL import Image as PILImage

        model, processor = self._load()
        start = time.perf_counter()
        with torch.inference_mode():
            pil_image = PILImage.fromarray(image).convert("RGB")
            pixel_values = processor(images=pil_image, return_tensors="pt").pixel_values
            generated_ids = model.generate(pixel_values)
            text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        latency = (time.perf_counter() - start) * 1000.0
        return OCRResult(text=text, latency_ms=latency)


BACKENDS = {"PP-OCRv5-mobile": PaddleOCRv5, "ThaiTrOCR": ThaiTrOCR}


def get_ocr(model: str, device: str = "cpu") -> OCRBackend:
    if model not in BACKENDS:
        raise ValueError(f"unknown OCR model {model!r}; expected one of {sorted(BACKENDS)}")
    return BACKENDS[model](device=device)
