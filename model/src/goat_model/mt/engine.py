"""Machine-translation backends for NLLB-200 distilled models.

Heavy frameworks (torch/transformers/ctranslate2) are imported lazily so the
base `uv sync` environment can still run smoke tests and data-prep scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from goat_model.constants import SEED
from goat_model.utils import have, log_call, resolve_device

if TYPE_CHECKING:
    import ctranslate2
    from transformers import PreTrainedTokenizerBase

NLLB_HF_IDS = {
    "NLLB-200-distilled-600M": "facebook/nllb-200-distilled-600M",
    "NLLB-200-distilled-1.3B": "facebook/nllb-200-distilled-1.3B",
}


@dataclass
class MTResult:
    translations: list[str]
    latency_ms: float


@runtime_checkable
class MTBackend(Protocol):
    def translate(self, sentences: list[str]) -> MTResult: ...

    def n_tokens(self, texts: list[str]) -> int: ...


class NLLBTransformers(MTBackend):
    """Reference implementation used as the ground truth for selection."""

    def __init__(
        self,
        model_name: str,
        src_lang: str,
        tgt_lang: str,
        beam: int = 4,
        max_length: int = 256,
        length_penalty: float = 1.0,
        device: str = "cuda",
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


def _decode_hypothesis(tokenizer: PreTrainedTokenizerBase, hypothesis: list[str]) -> str:
    """Drop the target-language token CT2 echoes back before the real text."""
    return tokenizer.decode(tokenizer.convert_tokens_to_ids(hypothesis[1:]), skip_special_tokens=True)


class NLLBCTranslate2(MTBackend):
    """The CTranslate2 artifact the app loads, graded through the same protocol.

    Tokenization stays with the HF tokenizer: the converted directory holds
    weights only, and NLLB's language tokens are a tokenizer concern. Scoring
    the shipped file is the point, so a difference against NLLBTransformers is
    a real difference in the artifact, not in how it is driven.
    """

    _translator: ctranslate2.Translator | None = None
    _tokenizer: PreTrainedTokenizerBase | None = None
    _options: ctranslate2.TranslationOptions | None = None

    def __init__(
        self,
        model_path: Path,
        tokenizer_id: str,
        src_lang: str,
        tgt_lang: str,
        beam: int = 4,
        max_length: int = 256,
        length_penalty: float = 1.0,
        device: str = "cpu",
        seed: int = SEED,
    ) -> None:
        if not model_path.is_dir():
            raise RuntimeError(
                f"CTranslate2 model dir {model_path} not found - run scripts/export_models.py first"
            )
        self.model_path = model_path
        self.tokenizer_id = tokenizer_id
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.beam = beam
        self.max_length = max_length
        self.length_penalty = length_penalty
        self.device = resolve_device(device)
        self.seed = seed

    def _load(self) -> tuple[ctranslate2.Translator, PreTrainedTokenizerBase]:
        if self._translator is None:
            try:
                import ctranslate2
                from transformers import AutoTokenizer
            except ImportError as err:
                raise RuntimeError(
                    "ctranslate2/transformers not installed — run `uv sync --extra mt`"
                ) from err
            self._tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_id, src_lang=self.src_lang)
            self._translator = ctranslate2.Translator(str(self.model_path), device=self.device)
            self._options = ctranslate2.TranslationOptions(
                beam_size=self.beam,
                max_decoding_length=self.max_length,
                length_penalty=self.length_penalty,
            )
        if self._tokenizer is None or self._options is None:
            raise RuntimeError("CTranslate2 backend state is incomplete")
        return self._translator, self._tokenizer

    def translate(self, sentences: list[str]) -> MTResult:
        import time

        if not sentences:
            return MTResult(translations=[], latency_ms=0.0)
        translator, tokenizer = self._load()
        sources = [tokenizer.convert_ids_to_tokens(tokenizer.encode(s)) for s in sentences]
        prefixes = [[self.tgt_lang] for _ in sentences]
        start = time.perf_counter()
        results = translator.translate_batch(sources, target_prefix=prefixes, options=self._options)
        latency = (time.perf_counter() - start) * 1000.0
        translations = [_decode_hypothesis(tokenizer, r.hypotheses[0]) for r in results]
        return MTResult(translations=translations, latency_ms=latency)

    def n_tokens(self, texts: list[str]) -> int:
        """Count tokenizer pieces, excluding special tokens (same metric as transformers)."""
        _, tokenizer = self._load()
        encoded = tokenizer(texts, add_special_tokens=False)
        return sum(len(ids) for ids in encoded["input_ids"])


@log_call
def get_mt(
    model_name: str,
    src_lang: str,
    tgt_lang: str,
    beam: int = 4,
    max_length: int = 256,
    length_penalty: float = 1.0,
    device: str = "cuda",
    seed: int = SEED,
    model_path: Path | None = None,
) -> MTBackend:
    if model_name not in NLLB_HF_IDS:
        raise ValueError(f"unknown MT model {model_name!r}; expected one of {sorted(NLLB_HF_IDS)}")
    device = resolve_device(device)
    if model_path is not None:
        return NLLBCTranslate2(
            model_path=model_path,
            tokenizer_id=NLLB_HF_IDS[model_name],
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            beam=beam,
            max_length=max_length,
            length_penalty=length_penalty,
            device=device,
            seed=seed,
        )
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
