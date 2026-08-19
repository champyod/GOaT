from __future__ import annotations

import jiwer
import numpy as np
import sacrebleu

from goat_model.constants import SEED


def cer(reference: str, hypothesis: str) -> float:
    return float(jiwer.cer(reference, hypothesis))


def word_accuracy(reference: str, hypothesis: str) -> float:
    return float(1.0 - jiwer.wer(reference, hypothesis))


def corpus_bleu(references: list[str], hypotheses: list[str]) -> float:
    return float(sacrebleu.corpus_bleu(hypotheses, [references]).score)


def summarize(runs: list[float]) -> tuple[float, float]:
    arr = np.asarray(runs, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1))


def paired_t_test(a: list[float], b: list[float], alpha: float = 0.05) -> dict:
    from scipy import stats

    t_stat, p_value = stats.ttest_rel(a, b)
    return {
        "t_statistic": float(t_stat),
        "p_value": float(p_value),
        "significant": bool(p_value < alpha),
    }


def cohens_d(a: list[float], b: list[float]) -> float:
    arr_a, arr_b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    pooled = np.sqrt((arr_a.std(ddof=1) ** 2 + arr_b.std(ddof=1) ** 2) / 2.0)
    if pooled == 0:
        return 0.0
    return float((arr_a.mean() - arr_b.mean()) / pooled)


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
