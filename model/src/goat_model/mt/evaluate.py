"""MT evaluation: corpus / per-domain BLEU, throughput, latency.

Throughput counts real tokenizer pieces of the hypotheses via the backend
(whitespace splitting is meaningless for Thai), reported as tokens/s.

Input files follow the analysis.typ contract:

    data/mt/test/flores200.th   (source sentences, one per line)
    data/mt/test/flores200.en   (reference sentences, one per line)
    data/mt/test/domains.txt    (optional: one domain tag per line, same order)
"""

from __future__ import annotations

from pathlib import Path

from goat_model.constants import SEED
from goat_model.mt.engine import MTBackend
from goat_model.utils import LogProgress


def load_pairs(
    src: Path, ref: Path, domains: Path | None = None
) -> tuple[list[str], list[str], list[str | None]]:
    from goat_model.utils import read_sentences

    sources = read_sentences(src)
    refs = read_sentences(ref)
    if len(sources) != len(refs):
        raise ValueError(f"src/ref length mismatch: {len(sources)} vs {len(refs)}")
    if domains is None or not domains.is_file():
        tags: list[str | None] = [None] * len(sources)
    else:
        tags = [
            line.strip()
            for line in domains.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(tags) != len(sources):
            raise ValueError(f"domains file has {len(tags)} rows but {len(sources)} sentences")
    return sources, refs, tags


def run_mt(
    backend: MTBackend, sources: list[str], refs: list[str], batch_size: int = 16, seed: int = SEED
) -> dict:
    import numpy as np

    from goat_model.metrics import corpus_bleu
    from goat_model.utils import setup_seed

    setup_seed(seed)
    hypotheses: list[str] = []
    per_batch_ms: list[float] = []
    batches = range(0, len(sources), batch_size)
    prog = LogProgress(len(batches), "mt-eval", unit="batch", interval_s=10.0, out_path=f"{len(sources)} sents")
    for i in batches:
        batch = sources[i : i + batch_size]
        result = backend.translate(batch)
        hypotheses.extend(result.translations)
        per_batch_ms.append(result.latency_ms)
        prog.update()
    prog.close()

    if len(hypotheses) != len(sources):
        raise RuntimeError(f"expected {len(sources)} translations, got {len(hypotheses)}")

    bleu = corpus_bleu(refs, hypotheses)
    tokens = backend.n_tokens(hypotheses)
    total_s = float(np.sum(per_batch_ms)) / 1000.0

    return {
        "bleu": bleu,
        "throughput_tokens_per_s": tokens / total_s if total_s > 0 else 0.0,
        "total_latency_ms": float(np.sum(per_batch_ms)),
        "average_ms_per_sentence": (float(np.sum(per_batch_ms)) / len(sources) if sources else 0.0),
        "hypotheses": hypotheses,
    }


def per_domain_bleu(
    refs: list[str], hypotheses: list[str], tags: list[str | None], seed: int = SEED
) -> dict[str, float]:
    from goat_model.metrics import corpus_bleu
    from goat_model.utils import setup_seed

    setup_seed(seed)
    grouped_refs: dict[str, list[str]] = {}
    grouped_hyp: dict[str, list[str]] = {}
    for ref, hyp, tag in zip(refs, hypotheses, tags):
        key = tag if tag else "overall"
        grouped_refs.setdefault(key, []).append(ref)
        grouped_hyp.setdefault(key, []).append(hyp)

    return {key: corpus_bleu(grouped_refs[key], grouped_hyp[key]) for key in grouped_refs}
