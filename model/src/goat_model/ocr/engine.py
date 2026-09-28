"""CPU OCR backends.

Heavy frameworks (paddle/onnxruntime/torch) are imported lazily so that
`uv sync` with only base deps can still run utils/metrics/smoke tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from goat_model.constants import SEED
from goat_model.utils import have, log_call, resolve_device


@dataclass
class OCRResult:
    text: str
    latency_ms: float


class OCRBackend(Protocol):
    def recognize(self, image: np.ndarray) -> OCRResult: ...


class PaddleOCRv5(OCRBackend):
    """PaddleOCR PP-OCRv5-mobile (detection + recognition, CPU).

    Built against the PaddleOCR 3.x pipeline API: predict() returns Result
    objects whose text lives in rec_texts.
    """

    name = "PP-OCRv5-mobile"

    def __init__(self, device: str = "cuda", seed: int = SEED) -> None:
        self.device = resolve_device(device)
        self.seed = seed
        self._engine = None

    def _load(self):
        if self._engine is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as err:
                raise RuntimeError("paddleocr not installed — run `uv sync --extra ocr`") from err
            import paddle

            paddle.seed(self.seed)
            kwargs = {"ocr_version": "PP-OCRv5", "lang": "th"}
            if self.device != "cpu":
                # PaddleX names it "gpu", not "cuda".
                kwargs["device"] = "gpu" if self.device == "cuda" else self.device
            self._engine = PaddleOCR(**kwargs)
        return self._engine

    def recognize(self, image: np.ndarray) -> OCRResult:
        import time

        engine = self._load()
        start = time.perf_counter()
        result = engine.predict(image)
        latency = (time.perf_counter() - start) * 1000.0
        text = "\n".join(self._page_texts(result))
        return OCRResult(text=text, latency_ms=latency)

    @staticmethod
    def _page_texts(result) -> list[str]:
        texts: list[str] = []
        for page in result:
            data = page.json if hasattr(page, "json") else page
            res = data.get("res", data) if isinstance(data, dict) else {}
            texts.extend(str(t) for t in res.get("rec_texts", []))
        return texts


class ThaiTrOCR(OCRBackend):
    """OpenThaiGPT ThaiTrOCR (Vision Transformer encoder + Electra decoder).

    Loader follows the official model card "How to Use":
    https://huggingface.co/openthaigpt/thai-trocr
    """

    name = "ThaiTrOCR"
    model_id = "openthaigpt/thai-trocr"
    img_size = 384

    def __init__(self, device: str = "cuda", seed: int = SEED) -> None:
        self.device = resolve_device(device)
        self.seed = seed
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
            self._model = self._model.to(self.device)
            setup_seed(self.seed)
        return self._model, self._processor

    def recognize(self, image: np.ndarray) -> OCRResult:
        import time

        import torch
        from PIL import Image as PILImage

        model, processor = self._load()
        start = time.perf_counter()
        with torch.inference_mode():
            pil_image = PILImage.fromarray(image).convert("RGB")
            pixel_values = processor(images=pil_image, return_tensors="pt").pixel_values.to(
                self.device
            )
            generated_ids = model.generate(pixel_values)
            text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        latency = (time.perf_counter() - start) * 1000.0
        return OCRResult(text=text, latency_ms=latency)


def _row_tol(boxes: list[tuple[int, int, int, int]]) -> int:
    """Row band height for top-to-bottom line sorting.

    Lines on one row share a y-center within ~half a line height; anything
    further apart is the next row. Falls back to 10px on empty input.
    """
    hs = sorted(b[3] - b[1] for b in boxes)
    return max(1, (hs[len(hs) // 2] if hs else 10) // 2)


class PaddleLineDetector:
    """PP-OCRv5_mobile_det text-line boxes (PaddleX single-model API)."""

    model_name = "PP-OCRv5_mobile_det"
    min_score = 0.5  # drop speckle boxes; Paddle's own box_thresh defaults ~0.6

    def __init__(self, device: str = "cuda", seed: int = SEED) -> None:
        self.device = resolve_device(device)
        self.seed = seed
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from paddlex import create_model
            except ImportError as err:
                raise RuntimeError("paddlex not installed — run `uv sync --extra ocr`") from err
            import paddle

            paddle.seed(self.seed)
            # PaddleX names it "gpu", not "cuda" (same mapping as PaddleOCRv5).
            self._model = create_model(
                model_name=self.model_name,
                device="gpu" if self.device == "cuda" else self.device,
            )
        return self._model

    def detect(self, image: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Axis-aligned (x0, y0, x1, y1) line boxes, top-to-bottom."""
        model = self._load()
        boxes: list[tuple[int, int, int, int]] = []
        for page in model.predict(image, batch_size=1):
            data = page.json if hasattr(page, "json") else page
            res = data.get("res", data) if isinstance(data, dict) else {}
            polys = res.get("dt_polys", [])
            scores = list(res.get("dt_scores", []))
            for poly, score in zip(polys, scores):
                if float(score) < self.min_score:
                    continue
                xs = [int(p[0]) for p in poly]
                ys = [int(p[1]) for p in poly]
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
        tol = _row_tol(boxes)
        boxes.sort(key=lambda b: ((b[1] + b[3]) // 2 // tol, b[0]))
        return boxes


class HybridLineOCR(OCRBackend):
    """Detector crops lines, ThaiTrOCR reads each crop.

    ThaiTrOCR is a single-line reader: a whole multi-line page squashed to
    384px is illegible to it (it hallucinates one short phrase). The PP-OCRv5
    detector finds the lines; each crop reaches ThaiTrOCR at native
    resolution, which is what the recognizer was built for.
    """

    name = "PPDet-ThaiTrOCR"
    box_pad_px = 2  # detector boxes hug glyphs; padding restores ascenders

    def __init__(
        self,
        device: str = "cuda",
        seed: int = SEED,
        detector: PaddleLineDetector | None = None,
        recognizer: ThaiTrOCR | None = None,
    ) -> None:
        self.device = resolve_device(device)
        self.seed = seed
        self._detector = detector
        self._recognizer = recognizer

    def recognize(self, image: np.ndarray) -> OCRResult:
        import time

        det = self._detector or PaddleLineDetector(device=self.device, seed=self.seed)
        rec = self._recognizer or ThaiTrOCR(device=self.device, seed=self.seed)
        h, w = image.shape[:2]
        start = time.perf_counter()
        boxes = det.detect(image)
        if not boxes:
            # Blank page or detector miss: whole image, same as ThaiTrOCR alone.
            out = rec.recognize(image)
            return OCRResult(text=out.text, latency_ms=(time.perf_counter() - start) * 1000.0)
        lines: list[str] = []
        for x0, y0, x1, y1 in boxes:
            crop = image[
                max(0, y0 - self.box_pad_px) : min(h, y1 + self.box_pad_px),
                max(0, x0 - self.box_pad_px) : min(w, x1 + self.box_pad_px),
            ]
            if crop.shape[0] < 2 or crop.shape[1] < 2:
                continue  # degenerate box: processor would choke, no signal lost
            lines.append(rec.recognize(crop).text.strip())
        return OCRResult(text="\n".join(lines), latency_ms=(time.perf_counter() - start) * 1000.0)


def _is_thai(ch: str) -> bool:
    return "\u0e00" <= ch <= "\u0e7f"  # Thai block


def join_thai_spaces(text: str) -> str:
    """Drop spaces sitting between two Thai-block characters.

    Port of the sidecar-ocr `join_thai_spaces`: Tesseract separates Thai
    glyphs with spaces, but Thai runs without inter-word spaces, so those
    are noise. Spaces touching Latin, digits or punctuation are kept.
    """
    chars = list(text)
    return "".join(
        ch
        for i, ch in enumerate(chars)
        if ch != " "
        or i == 0
        or i == len(chars) - 1
        or not (_is_thai(chars[i - 1]) and _is_thai(chars[i + 1]))
    )


class TesseractOCR(OCRBackend):
    """System `tesseract` CLI (eng+tha, PSM 11): the desktop fallback, raced as-is.

    CPU-only; never trained (legacy tesstrain pipeline, not worth its cost
    for a fallback). stdlib subprocess only: no Python wrapper dependency,
    same binary the Rust sidecar shells out to.
    """

    name = "Tesseract"
    lang = "eng+tha"
    psm = "11"  # sparse text: screenshots are scattered fragments, not pages

    def __init__(self, device: str = "cpu", seed: int = SEED, runner=None) -> None:
        self.device = device
        self.seed = seed
        self._runner = runner

    def _run_cli(self, path: str) -> str:
        import subprocess

        proc = subprocess.run(
            ["tesseract", path, "stdout", "-l", self.lang, "--psm", self.psm],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,  # returncode handled below to include stderr
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract failed: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def recognize(self, image: np.ndarray) -> OCRResult:
        import tempfile
        import time

        import cv2

        runner = self._runner or self._run_cli
        start = time.perf_counter()
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
                cv2.imwrite(tmp.name, cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
                text = runner(tmp.name)
        except FileNotFoundError as err:
            raise RuntimeError(
                "tesseract binary not found — `apt install tesseract-ocr tesseract-ocr-tha`"
            ) from err
        lines = [join_thai_spaces(ln).rstrip() for ln in text.strip().splitlines()]
        return OCRResult(text="\n".join(lines), latency_ms=(time.perf_counter() - start) * 1000.0)


BACKENDS = {
    "PP-OCRv5-mobile": PaddleOCRv5,
    "ThaiTrOCR": ThaiTrOCR,
    HybridLineOCR.name: HybridLineOCR,
    TesseractOCR.name: TesseractOCR,
}


@log_call
def get_ocr(model: str, device: str = "cuda", seed: int = SEED) -> OCRBackend:
    if model not in BACKENDS:
        raise ValueError(f"unknown OCR model {model!r}; expected one of {sorted(BACKENDS)}")
    device = resolve_device(device)
    return BACKENDS[model](device=device, seed=seed)
