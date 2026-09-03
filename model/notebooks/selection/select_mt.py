#!/usr/bin/env python3
"""MT model selection: NLLB-200-distilled-600M vs 1.3B.

Applies the decision rule (hypothesis 3 / methodology):
pick 600M iff BLEU > 35 AND average latency <= 2 s per screen, else 1.3B.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import dataset_revisions
from tqdm import tqdm
from goat_model.metrics import cohens_d, paired_t_test, summarize
from goat_model.mt import evaluate
from goat_model.mt.engine import get_mt
from goat_model.utils import setup_seed, write_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MT selection experiment (BLEU/latency decision rule)."
    )
    parser.add_argument("--src", choices=("th", "en"), default="en")
    parser.add_argument("--tgt", choices=("th", "en"), default="th")
    parser.add_argument("--repeats", type=int, default=c.MT_N_RUNS)
    parser.add_argument("--mt-test-dir", type=Path, default=c.MT_TEST)
    parser.add_argument("--device", default="auto", help="auto=cuda if available else cpu")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "mt_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()

    setup_seed(args.seed)
    import torch
    device = args.device if args.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[select-mt] device={device}", flush=True)
    src_file = args.mt_test_dir / f"flores200.{args.src}"
    ref_file = args.mt_test_dir / f"flores200.{args.tgt}"
    domain_file = args.mt_test_dir / "flores200.domains"
    sources, refs, tags = evaluate.load_pairs(src_file, ref_file, domain_file)

    results: dict = {
        "runs": args.repeats,
        "seed": args.seed,
        "dataset_revisions": dataset_revisions(),
        "models": {},
        "comparisons": [],
    }
    bleu_series: dict[str, list[float]] = {}
    latency_series: dict[str, list[float]] = {}
    last_by_model: dict[str, dict] = {}

    for model in c.MT_MODELS:
        backend = get_mt(
            model_name=model,
            src_lang=c.LANG_CODES[args.src],
            tgt_lang=c.LANG_CODES[args.tgt],
            beam=c.MT_BEAM_SIZE,
            max_length=c.MT_MAX_LENGTH,
            length_penalty=c.MT_LENGTH_PENALTY,
            device=device,
            seed=args.seed,
        )
        bleu_series[model] = []
        latency_series[model] = []
        print(f"[select-mt] {model} — {args.repeats} repeats", flush=True)
        for rep in tqdm(range(args.repeats), desc=f"select-mt {model}", unit="repeat"):
            run = evaluate.run_mt(
                backend, sources, refs, batch_size=c.MT_BATCH_SIZE, seed=args.seed
            )
            bleu_series[model].append(run["bleu"])
            latency_series[model].append(run["average_ms_per_sentence"] / 1000.0)
            last_by_model[model] = run
        bleu_mean, bleu_std = summarize(bleu_series[model])
        lat_mean, lat_std = summarize(latency_series[model])
        results["models"][model] = {
            "bleu": {"mean": bleu_mean, "std": bleu_std},
            "avg_s_per_sentence": {"mean": lat_mean, "std": lat_std},
            "per_domain_bleu": evaluate.per_domain_bleu(
                refs, last_by_model[model]["hypotheses"], tags, seed=args.seed
            ),
        }

    a, b = c.MT_MODELS
    test = paired_t_test(bleu_series[a], bleu_series[b], alpha=c.MT_ALPHA)
    results["comparisons"].append(
        {
            "a": a,
            "b": b,
            "paired_t_test_bleu": test,
            "cohens_d_bleu": cohens_d(bleu_series[a], bleu_series[b]),
        }
    )

    m0 = results["models"]["NLLB-200-distilled-600M"]["bleu"]["mean"]
    lat0 = results["models"]["NLLB-200-distilled-600M"]["avg_s_per_sentence"]["mean"]
    selected = (
        "NLLB-200-distilled-600M"
        if m0 > c.MT_BLEU_THRESHOLD and lat0 <= c.MT_LATENCY_THRESHOLD_S
        else "NLLB-200-distilled-1.3B"
    )
    results["decision"] = {
        "rule": f"NLLB-600M iff BLEU > {c.MT_BLEU_THRESHOLD} and latency <= {c.MT_LATENCY_THRESHOLD_S}s",
        "600m_bleu": m0,
        "600m_avg_s": lat0,
        "selected": selected,
    }
    write_json(args.output, results)

    for model in c.MT_MODELS:
        m = results["models"][model]
        print(
            f"{model}: BLEU {m['bleu']['mean']:.2f}±{m['bleu']['std']:.2f} "
            f"| {m['avg_s_per_sentence']['mean'] * 1000:.0f}±{m['avg_s_per_sentence']['std'] * 1000:.0f} ms/sentence",
            flush=True
        )
    print(f"paired t-test p={test['p_value']:.4f} significant={test['significant']}", flush=True)
    print(f"SELECTED: {selected}", flush=True)
    print(f"wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()
