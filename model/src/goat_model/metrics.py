from __future__ import annotations

import numpy as np
import sacrebleu

from goat_model.constants import SEED
from goat_model.utils import log_call


def _levenshtein(a: str, b: str) -> tuple[int, int | None, int | None, int | None]:
    """(distance, substitutions, deletions, insertions) between `a` and `b`.

    Distance is computed with a two-row sweep (exact, O(n) memory). The
    operation breakdown comes from a traceback through the full DP matrix,
    which is only kept when the texts are small enough to bound memory;
    otherwise the op counts are None.
    """
    n, m = len(a), len(b)
    if n == 0:
        return m, 0, 0, m
    if m == 0:
        return n, 0, n, 0

    prev = list(range(m + 1))
    for i in range(1, n + 1):
        ai = a[i - 1]
        cur = [i]
        for j in range(1, m + 1):
            equals = ai == b[j - 1]
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if equals else 1)))
        prev = cur
    dist = prev[m]

    if n * m > 4_000_000:  # ~32 MB of matrix cells
        return dist, None, None, None

    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        ai = a[i - 1]
        di, di_1 = d[i], d[i - 1]
        for j in range(1, m + 1):
            equals = ai == b[j - 1]
            di[j] = min(di_1[j] + 1, di[j - 1] + 1, di_1[j - 1] + (0 if equals else 1))

    subs = dels = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            equal = a[i - 1] == b[j - 1]
            if d[i][j] == d[i - 1][j - 1] + (0 if equal else 1):
                subs += 0 if equal else 1
                i, j = i - 1, j - 1
                continue
        if i > 0 and d[i][j] == d[i - 1][j] + 1:
            dels += 1
            i -= 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ins += 1
            j -= 1
        else:
            raise AssertionError("unreachable traceback step")
    return dist, subs, dels, ins


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate in [0, 1]; 0.0 iff the texts are identical.

    Uses the bounded normalization dist / max(len(ref), len(hyp)): Levenshtein
    distance never exceeds the longer text's length, so the rate cannot leave
    [0, 1] regardless of how much extra text a model emits (unspaced Thai OCR
    used to score 2-3x "error" against jiwer's unbounded ref-normalized CER).
    """
    ref, hyp = reference.strip(), hypothesis.strip()
    dist, *_ = _levenshtein(ref, hyp)
    denom = max(len(ref), len(hyp))
    return 0.0 if denom == 0 else dist / denom


def trace_cer(reference: str, hypothesis: str) -> dict:
    """Every step of the CER calculation, for the per-run trace logs."""
    ref, hyp = reference.strip(), hypothesis.strip()
    dist, subs, dels, ins = _levenshtein(ref, hyp)
    denom = max(len(ref), len(hyp))
    value = 0.0 if denom == 0 else dist / denom
    return {
        "ref_len": len(ref),
        "hyp_len": len(hyp),
        "normalizer": denom,  # max(ref_len, hyp_len)
        "distance": dist,
        "substitutions": subs,
        "deletions": dels,
        "insertions": ins,
        "cer": value,
        "word_accuracy": 1.0 - value,
    }


def word_accuracy(reference: str, hypothesis: str) -> float:
    """Character-level accuracy = 1 - CER, in [0, 1].

    The old 1 - WER went negative because Thai has no inter-word spaces
    (whitespace "words" are degenerate); char-level is well-defined here.
    """
    return 1.0 - cer(reference, hypothesis)


@log_call
def corpus_bleu(references: list[str], hypotheses: list[str]) -> float:
    """Word-level BLEU on Thai-segmented text.

    sacrebleu's default 13a tokenizer reads unspaced Thai as one token
    per sentence, so n-gram counts are zero and BLEU collapses to ~0.
    Segment both sides with pythainlp newmm, then score the already
    space-separated tokens (tokenize="none").

    sacrebleu semantics: BLEU is only defined when the corpus contains a
    sentence with >= 4 word-tokens; otherwise its 4-gram precision is 0/0
    and the score is 0.0. Real eval sets (FLORES etc.) are fine; do not
    judge the metric on tiny or very short corpora.
    """
    from pythainlp.tokenize import word_tokenize

    def _words(text: str) -> list[str]:
        # newmm keeps literal " " tokens from the input; joining them yields
        # runs of spaces that break sacrebleu's n-gram counting (identical
        # inputs scored 0), so drop whitespace-only tokens.
        return [t for t in word_tokenize(text, engine="newmm") if t.strip()]

    refs = [" ".join(_words(r)) for r in references]
    hyps = [" ".join(_words(h)) for h in hypotheses]
    return float(sacrebleu.corpus_bleu(hyps, [refs], tokenize="none").score)


@log_call
def trace_corpus_bleu(references: list[str], hypotheses: list[str]) -> dict:
    """Corpus BLEU with its n-gram counts, for the per-run trace logs."""
    from pythainlp.tokenize import word_tokenize

    def _words(text: str) -> list[str]:
        return [t for t in word_tokenize(text, engine="newmm") if t.strip()]

    refs = [" ".join(_words(r)) for r in references]
    hyps = [" ".join(_words(h)) for h in hypotheses]
    res = sacrebleu.corpus_bleu(hyps, [refs], tokenize="none")
    return {
        "score": float(res.score),
        "counts": list(res.counts),
        "totals": list(res.totals),
        "precisions": list(res.precisions),
        "brevity_penalty": float(res.bp),
        "n_ref_tokens": sum(len(r.split()) for r in refs),
        "n_hyp_tokens": sum(len(h.split()) for h in hyps),
    }


@log_call
def summarize(runs: list[float]) -> tuple[float, float]:
    arr = np.asarray(runs, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1))


@log_call
def paired_t_test(a: list[float], b: list[float], alpha: float = 0.05) -> dict:
    from scipy import stats

    t_stat, p_value = stats.ttest_rel(a, b)
    return {
        "t_statistic": float(t_stat),
        "p_value": float(p_value),
        "significant": bool(p_value < alpha),
    }


@log_call
def cohens_d(a: list[float], b: list[float]) -> float:
    arr_a, arr_b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    pooled = np.sqrt((arr_a.std(ddof=1) ** 2 + arr_b.std(ddof=1) ** 2) / 2.0)
    if pooled == 0:
        return 0.0
    return float((arr_a.mean() - arr_b.mean()) / pooled)


@log_call
def bootstrap_ci(
    samples: list[float], n_boot: int = 10_000, seed: int = SEED, ci: float = 0.95
) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    arr = np.asarray(samples, dtype=float)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(arr, size=len(arr), replace=True).mean()
    lo = float(np.percentile(means, (1 - ci) / 2 * 100))
    hi = float(np.percentile(means, (1 + ci) / 2 * 100))
    return {"ci_low": lo, "ci_high": hi, "ci_level": ci, "n_boot": n_boot}
