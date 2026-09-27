#!/usr/bin/env python3
"""OCR model selection: PP-OCRv5-mobile vs ThaiTrOCR.

Runs both models on both public datasets for `--repeats` runs, reports
mean±std + 95% CI, then applies the decision rule (hypothesis 2 / methodology):
pick ThaiTrOCR iff its CER <= 0.10, otherwise pick the lowest CER model.
"""

from __future__ import annotations

import argparse
import itertools
import traceback
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import dataset_revisions
from goat_model.metrics import cer, cohens_d, order_hypothesis, paired_t_test, trace_cer
from goat_model.ocr import evaluate
from goat_model.ocr.engine import get_ocr
from goat_model.log import dump as _dump
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import (
    LogProgress,
    load_dotenv,
    log_call,
    parse_subset_arg,
    resolve_device,
    setup_seed,
    write_json,
)


@log_call
def _partial_path(output: Path) -> Path:
    return output.with_name(output.stem + ".partial.json")


@log_call
def _model_file(output: Path, model: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_")
    return output.with_name(f"{output.stem}.{safe}.json")


@log_call
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


@log_call
def _recs(triples: list) -> list[dict]:
    return [{"cer": c, "word_accuracy": w, "latency_ms": m} for c, w, m in triples]


@log_call
def _samples_path(output: Path, run_id: str) -> Path:
    return output.with_name(f"{output.stem}.samples_{run_id}.jsonl")


def _result_snapshot(output: Path, run_id: str) -> Path:
    return output.with_name(f"{output.stem}.{run_id}.result.json")


def _dump_samples(path: Path, rows: list[dict]) -> None:
    """Append expected-vs-got rows to the per-run sample log (never overwritten)."""
    try:
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as err:
        _warn("select-ocr", "sample log write failed", path=str(path), error=str(err))


@log_call
def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="OCR selection experiment (CER decision rule).")
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    parser.add_argument("--ocr-eval-dir", type=Path, default=c.OCR_EVAL)
    parser.add_argument(
        "--device",
        default="cuda",
        help="cuda (default, fails fast if unavailable) | cpu (explicit, slow)",
    )
    parser.add_argument(
        "--force", action="store_true", help="ignore checkpoints, rerun all repeats"
    )
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    parser.add_argument(
        "--models",
        default=",".join(c.OCR_MODELS),
        help="comma-separated model subset for parallel workers (default: all)",
    )
    parser.add_argument(
        "--datasets",
        default=",".join(c.OCR_DATASETS),
        help="comma-separated dataset subset for parallel workers (default: all)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=None,
        help="cap images per dataset (deterministic first-N smoke runs; verdict needs full)",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="answer key is bottom-to-top (render order): flip hypothesis lines before scoring",
    )
    args = parser.parse_args()
    _info("select-ocr", "args", **vars(args))
    models = parse_subset_arg(args.models, c.OCR_MODELS, "model")
    datasets = parse_subset_arg(args.datasets, c.OCR_DATASETS, "dataset")
    _err_out = args.output
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    samples_path = _samples_path(args.output, run_id)
    try:
        if not args.force and args.output.is_file():
            _info(
                "select-ocr",
                "skipped - already selected (use --force to rerun)",
                out=str(args.output),
            )
            return

        setup_seed(args.seed)
        device = resolve_device(args.device)
        _info("select-ocr", "device", device=device)
        results: dict = {
            "runs": args.repeats,
            "seed": args.seed,
            "reverse": args.reverse,
            "dataset_revisions": dataset_revisions(),
            "models": {},
            "comparisons": [],
        }

        cer_by_model: dict[str, list[float]] = {}
        partial_path = _partial_path(args.output)
        saved = {} if args.force else _load_partial(partial_path, args.seed, args.repeats)
        saved_runs = saved.get("runs_data", {})
        if saved:
            _info("select-ocr", "resuming", partial=str(partial_path))
        for model in models:
            stats = {}
            for dataset in datasets:
                dataset_dir = args.ocr_eval_dir / dataset
                assets = evaluate.discover_assets(dataset_dir)
                if args.max_images is not None:
                    # discover_assets sorts: first-N is deterministic across workers.
                    assets = assets[: args.max_images]
                    _info("select-ocr", "capped images", n=len(assets), dataset=dataset)
                backend = get_ocr(model, device=device, seed=args.seed)
                img_size = c.OCR_IMG_SIZE[model]
                _info(
                    "select-ocr",
                    "loading weights (first run downloads GBs)",
                    model=model,
                    dataset=dataset,
                )
                key = f"{model}/{dataset}"
                stored = [list(r) for r in saved_runs.get(key, [])]
                done = len(stored)
                prog = LogProgress(
                    args.repeats, f"select-ocr {model}/{dataset}", unit="repeat", interval_s=30.0
                )
                prog.n = done
                runs = [_recs(r) for r in stored]
                triples = [list(r) for r in stored]
                for i in range(done, args.repeats):
                    recs = evaluate.run_ocr(backend, assets, img_size, seed=args.seed)
                    if args.reverse:
                        # Bottom-to-top answer key: reorder before ANY scoring,
                        # logging, or checkpointing so all three agree.
                        for rec in recs:
                            hyp = order_hypothesis(rec["hypothesis"], reverse=True)
                            rec["hypothesis"] = hyp
                            rec["cer"] = cer(rec["reference"], hyp)
                            rec["word_accuracy"] = 1.0 - rec["cer"]
                    runs.append(recs)
                    triples.append(
                        [[rec["cer"], rec["word_accuracy"], rec["latency_ms"]] for rec in recs]
                    )
                    _dump_samples(
                        samples_path,
                        [
                            {
                                "repeat_index": i,
                                "model": model,
                                "dataset": dataset,
                                **rec,
                                "trace": trace_cer(rec["reference"], rec["hypothesis"]),
                                "verdict": "pass" if rec["cer"] <= c.OCR_CER_THRESHOLD else "fail",
                            }
                            for rec in recs
                        ],
                    )
                    saved_runs[key] = triples
                    write_json(
                        partial_path,
                        {"seed": args.seed, "runs": args.repeats, "runs_data": saved_runs},
                    )
                    prog.update()
                prog.close()
                summary = evaluate.aggregate_records(runs)
                stats[dataset] = summary
                cer_by_model.setdefault(model, []).extend(rec["cer"] for run in runs for rec in run)
            results["models"][model] = stats
            write_json(
                _model_file(args.output, model), {"model": model, "seed": args.seed, **stats}
            )

        mean_cer = {m: sum(v) / len(v) for m, v in cer_by_model.items()}
        for a, b in itertools.combinations(models, 2):
            results["comparisons"].append(
                {
                    "a": a,
                    "b": b,
                    "paired_t_test": paired_t_test(
                        cer_by_model[a], cer_by_model[b], alpha=c.OCR_ALPHA
                    ),
                    "cohens_d": cohens_d(cer_by_model[a], cer_by_model[b]),
                }
            )

        # Pure lowest CER wins: any model may be frozen (PP-OCRv5, Tesseract)
        # or trainable (ThaiTrOCR, hybrid's recognizer half); training handles
        # each winner accordingly, so the gate plays no favorites. Subset
        # workers cannot decide: merge their outputs, then train off the merge.
        full = set(models) == set(c.OCR_MODELS) and set(datasets) == set(c.OCR_DATASETS)
        decision = min(mean_cer, key=lambda m: mean_cer[m]) if full else None
        results["decision"] = {
            "rule": f"lowest mean CER over {', '.join(c.OCR_MODELS)}",
            "mean_cer": mean_cer,
            "selected": decision,
        }
        if not full:
            results["decision"]["note"] = "subset run — merge worker outputs first"
        write_json(args.output, results)
        snapshot = _result_snapshot(args.output, run_id)
        write_json(snapshot, results)
        _info("select-ocr", "wrote result snapshot", out=str(snapshot))
        partial_path.unlink(missing_ok=True)
        Path(str(args.output) + ".error.json").unlink(missing_ok=True)

        _info("select-ocr", "mean CER", **{m: round(v, 4) for m, v in mean_cer.items()})
        _info("select-ocr", "comparisons", n=len(results["comparisons"]))
        _info("select-ocr", "SELECTED", model=decision)
        _info("select-ocr", "wrote", out=str(args.output))

    except Exception as err:
        tb = traceback.format_exc()
        inp = args.ocr_eval_dir
        _err("select_ocr", "failed", inp=str(inp), out=str(_err_out), error=str(err))
        _dump("traceback", tb)
        if _err_out is not None:
            try:
                _err_path = str(_err_out) + ".error.json"
                from pathlib import Path as _P

                _P(_err_path).write_text(
                    json.dumps(
                        {
                            "error": str(err),
                            "kind": "select_ocr",
                            "input": str(inp),
                            "output": str(_err_out),
                        },
                        indent=2,
                    )
                )
                _info("select_ocr", "wrote error file", path=_err_path)
            except Exception:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
