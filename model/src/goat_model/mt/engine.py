"""Machine-translation backends for NLLB-200 distilled models.

Heavy frameworks (torch/transformers/ctranslate2) are imported lazily so the
base `uv sync` environment can still run smoke tests and data-prep scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from goat_model.constants import SEED
from goat_model.utils import have

NLLB_HF_IDS = {
    "NLLB-200-distilled-600M": "facebook/nllb-200-distilled-600M",
    "NLLB-200-distilled-1.3B": "facebook/nllb-200-distilled-1.3B",
}


@dataclass
class MTResult:
    translations: list[str]
    latency_ms: float


class MTBackend(Protocol):
    def translate(self, sentences: list[str]) -> MTResult: ...

    def n_tokens(self, texts: list[str]) -> int: ...


class NLLBTransformers(MTBackend):
    """Reference implementation used as the ground truth for selection.

    Replace with a CTranslate2 backend for the shipped app once the
    Transformers version is validated (see scripts/export_models.py).
    """

    def __init__(
        self,
        model_name: str,
        src_lang: str,
        tgt_lang: str,
        beam: int = 4,
        max_length: int = 256,
        length_penalty: float = 1.0,
        device: str = "cpu",
        seed: int = SEED,
    ) -> None:
        self.model_name = model_name
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.beam = beam
        self.max_length = max_length
        self.length_penalty = length_penalty
        self.device = device
        self.seed = seed
        self._model = None
        self._tokenizer = None

    def _load(self):
        if self._model is None:
            if not have("torch", "transformers"):
                raise RuntimeError("torch/transformers not installed — run `uv sync --extra mt`")
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            from goat_model.utils import setup_seed

            model_id = NLLB_HF_IDS[self.model_name]
            self._tokenizer = AutoTokenizer.from_pretrained(
                model_id, src_lang=self.src_lang, tgt_lang=self.tgt_lang
            )
            self._model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
            self._model.eval()
            self._model = self._model.to(self.device)
            setup_seed(self.seed)
        return self._model, self._tokenizer

    def translate(self, sentences: list[str]) -> MTResult:
        import time

        import torch

        if not sentences:
            return MTResult(translations=[], latency_ms=0.0)

        model, tokenizer = self._load()
        start = time.perf_counter()
        with torch.inference_mode():
            inputs = tokenizer(sentences, return_tensors="pt", padding=True, truncation=True).to(self.device)
            outputs = model.generate(
                **inputs,
                forced_bos_token_id=tokenizer.convert_tokens_to_ids(self.tgt_lang),
                num_beams=self.beam,
                max_length=self.max_length,
                length_penalty=self.length_penalty,
            )
            translations = [tokenizer.decode(out, skip_special_tokens=True) for out in outputs]
        latency = (time.perf_counter() - start) * 1000.0
        return MTResult(translations=translations, latency_ms=latency)

    def n_tokens(self, texts: list[str]) -> int:
        """Count tokenizer pieces, excluding special tokens."""
        _, tokenizer = self._load()
        encoded = tokenizer(texts, add_special_tokens=False)
        return sum(len(ids) for ids in encoded["input_ids"])


def get_mt(
    model_name: str,
    src_lang: str,
    tgt_lang: str,
    beam: int = 4,
    max_length: int = 256,
    length_penalty: float = 1.0,
    device: str = "cpu",
    seed: int = SEED,
) -> MTBackend:
    if model_name not in NLLB_HF_IDS:
        raise ValueError(f"unknown MT model {model_name!r}; expected one of {sorted(NLLB_HF_IDS)}")
    return NLLBTransformers(
        model_name=model_name,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        beam=beam,
        max_length=max_length,
        length_penalty=length_penalty,
        device=device,
        seed=seed,
    )
