#!/usr/bin/env python3
"""MT evaluation.

Usage:
    python scripts/eval_mt.py --model NLLB-200-distilled-600M \
        --dataset flores200 --output ./results/mt.json --device cpu \
        --beam 4 --max_len 256 --seed 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.metrics import bootstrap_ci, summarize
from goat_model.mt import evaluate
from goat_model.mt.engine import get_mt
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, resolve_device, setup_seed, write_json


@log_call
def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an NLLB-200 model on FLORES-200.")
    parser.add_argument("--model", choices=c.MT_MODELS, default="NLLB-200-distilled-600M")
    parser.add_argument("--src", choices=("th", "en"), default="en")
    parser.add_argument("--tgt", choices=("th", "en"), default="th")
    parser.add_argument("--dataset", choices=("flores200",), default="flores200")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda", help="cuda (default, fails fast if unavailable) | cpu (explicit, slow)")
    parser.add_argument("--beam", type=int, default=c.MT_BEAM_SIZE)
    parser.add_argument("--max_len", type=int, default=c.MT_MAX_LENGTH)
    parser.add_argument("--length_penalty", type=float, default=c.MT_LENGTH_PENALTY)
    parser.add_argument("--batch_size", type=int, default=c.MT_BATCH_SIZE)
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--repeats", type=int, default=c.MT_N_RUNS)
    args = parser.parse_args()
    args.device = resolve_device(args.device)

    setup_seed(args.seed)
    src_file = c.MT_TEST / f"{args.dataset}.{args.src}"
    ref_file = c.MT_TEST / f"{args.dataset}.{args.tgt}"
    domain_file = c.MT_TEST / f"{args.dataset}.domains"
    if not src_file.is_file() or not ref_file.is_file():
        parser.error(
            f"missing {src_file} or {ref_file} - run scripts/download_data.py --dataset {args.dataset}"
        )

    src_lang = c.LANG_CODES[args.src]
    tgt_lang = c.LANG_CODES[args.tgt]
    sources, refs, tags = evaluate.load_pairs(src_file, ref_file, domain_file)
    _info("eval-mt", "loaded pairs", model=args.model, pairs=len(sources), src=args.src, tgt=args.tgt, device=args.device)

    backend = get_mt(
        model_name=args.model,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        beam=args.beam,
        max_length=args.max_len,
        length_penalty=args.length_penalty,
        device=args.device,
        seed=args.seed,
    )

    run_metrics: dict[str, list[float]] = {"bleu": [], "throughput_tokens_per_s": []}
    last = None
    for _ in range(args.repeats):
        last = evaluate.run_mt(
            backend, sources, refs, batch_size=args.batch_size, seed=args.seed
        )
        run_metrics["bleu"].append(last["bleu"])
        run_metrics["throughput_tokens_per_s"].append(last["throughput_tokens_per_s"])

    summary = {
        metric: {"mean": summarize(vals)[0], "std": summarize(vals)[1], "ci95": bootstrap_ci(vals)}
        for metric, vals in run_metrics.items()
    }
    report = {
        "model": args.model,
        "src": args.src,
        "tgt": args.tgt,
        "dataset": args.dataset,
        "device": args.device,
        "beam": args.beam,
        "max_len": args.max_len,
        "n_runs": args.repeats,
        "n_sentences": len(sources),
        "metrics": summary,
        "per_domain_bleu": evaluate.per_domain_bleu(refs, last["hypotheses"], tags),
        "last_run": last,
    }
    write_json(args.output, report)

    for metric, vals in summary.items():
        mean = f"{vals['mean']:.2f}".rstrip("0").rstrip(".")
        std = f"{vals['std']:.2f}".rstrip("0").rstrip(".")
        ci = (vals["ci95"]["ci_low"], vals["ci95"]["ci_high"])
        _info("eval-mt", "metric", metric=metric, mean=mean, std=std, ci_low=round(ci[0], 2), ci_high=round(ci[1], 2))
    _info("eval-mt", "wrote", out=str(args.output))


if __name__ == "__main__":
    main()
