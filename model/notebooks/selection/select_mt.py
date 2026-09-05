#!/usr/bin/env python3
"""MT model selection: NLLB-200-distilled-600M vs 1.3B.

Applies the decision rule (hypothesis 3 / methodology):
pick 600M iff BLEU > 35 AND average latency <= 2 s per screen, else 1.3B.
"""

from __future__ import annotations

import argparse
import traceback
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import dataset_revisions
from goat_model.metrics import cohens_d, paired_t_test, summarize
from goat_model.mt import evaluate
from goat_model.mt.engine import get_mt
from goat_model.utils import LogProgress, setup_seed, write_json


def _partial_path(output: Path) -> Path:
    return output.with_name(output.stem + ".partial.json")


def _model_file(output: Path, model: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_")
    return output.with_name(f"{output.stem}.{safe}.json")


def _load_partial(path: Path, seed: int, repeats: int) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("seed") != seed or data.get("runs") != repeats:
        return {}
    return data


def _flush_partial(path: Path, seed: int, repeats: int, bleu, lat, hyp) -> None:
    write_json(path, {"seed": seed, "runs": repeats, "bleu_series": bleu, "latency_series": lat, "last_hyp": hyp})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MT selection experiment (BLEU/latency decision rule)."
    )
    parser.add_argument("--src", choices=("th", "en"), default="en")
    parser.add_argument("--tgt", choices=("th", "en"), default="th")
    parser.add_argument("--repeats", type=int, default=c.MT_N_RUNS)
    parser.add_argument("--batch", type=int, default=c.MT_BATCH_SIZE, help="eval batch size (steps = ceil(len(test)/batch))")
    parser.add_argument("--mt-test-dir", type=Path, default=c.MT_TEST)
    parser.add_argument("--device", default="auto", help="auto=cuda if available else cpu")
    parser.add_argument("--force", action="store_true", help="ignore checkpoints, rerun all repeats")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "mt_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    args = parser.parse_args()
    _err_out = args.output
    try:
        if not args.force and args.output.is_file():
            print(f"skipped - already selected: {args.output} (use --force to rerun)", flush=True)
            return

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
        partial_path = _partial_path(args.output)
        saved = {} if args.force else _load_partial(partial_path, args.seed, args.repeats)
        saved_bleu = saved.get("bleu_series", {})
        saved_lat = saved.get("latency_series", {})
        saved_hyp = saved.get("last_hyp", {})
        if saved:
            print(f"[select-mt] resuming from {partial_path}", flush=True)

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
            bleu_series[model] = list(saved_bleu.get(model, []))
            latency_series[model] = list(saved_lat.get(model, []))
            done = len(bleu_series[model])
            last_by_model[model] = {"hypotheses": saved_hyp.get(model, [])}
            print(f"[select-mt] {model} — loading weights (first run downloads GBs)", flush=True)
            prog = LogProgress(args.repeats, f"select-mt {model}", unit="repeat", interval_s=30.0)
            prog.n = done
            for rep in range(done, args.repeats):
                run = evaluate.run_mt(
                    backend, sources, refs, batch_size=args.batch, seed=args.seed
                )
                bleu_series[model].append(run["bleu"])
                latency_series[model].append(run["average_ms_per_sentence"] / 1000.0)
                last_by_model[model] = run
                _flush_partial(partial_path, args.seed, args.repeats, bleu_series, latency_series, {m: last_by_model[m]["hypotheses"] for m in last_by_model if last_by_model[m].get("hypotheses")})
                prog.update()
            prog.close()
            bleu_mean, bleu_std = summarize(bleu_series[model])
            lat_mean, lat_std = summarize(latency_series[model])
            results["models"][model] = {
                "bleu": {"mean": bleu_mean, "std": bleu_std},
                "avg_s_per_sentence": {"mean": lat_mean, "std": lat_std},
                "per_domain_bleu": evaluate.per_domain_bleu(
                    refs, last_by_model[model]["hypotheses"], tags, seed=args.seed
                ),
            }
            write_json(_model_file(args.output, model), {"model": model, "seed": args.seed, **results["models"][model]})

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
        partial_path.unlink(missing_ok=True)

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


    except Exception as err:
        tb = traceback.format_exc()
        inp = args.mt_test_dir
        print(f"[error] select_mt failed | in={inp} out={_err_out} | {err}", flush=True)
        print(tb, flush=True)
        if _err_out is not None:
            try:
                _err_path = str(_err_out) + ".error.json"
                from pathlib import Path as _P
                _P(_err_path).write_text(json.dumps({"error": str(err), "kind": "select_mt", "input": str(inp), "output": str(_err_out)}, indent=2))
                print(f"[error] wrote {{_err_path}}", flush=True)
            except Exception:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
