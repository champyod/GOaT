#!/usr/bin/env python3
"""OCR model selection: PP-OCRv5-mobile vs ThaiTrOCR.

Runs both models on both public datasets for `--repeats` runs, reports
mean±std + 95% CI, then applies the decision rule (hypothesis 2 / methodology):
pick ThaiTrOCR iff its CER <= 0.10, otherwise pick the lowest CER model.
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
from goat_model.metrics import cohens_d, paired_t_test
from goat_model.ocr import evaluate
from goat_model.ocr.engine import get_ocr
from goat_model.log import dump as _dump
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import LogProgress, load_dotenv, log_call, resolve_device, setup_seed, write_json


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
def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="OCR selection experiment (CER decision rule).")
    parser.add_argument("--repeats", type=int, default=c.OCR_N_RUNS)
    parser.add_argument("--ocr-eval-dir", type=Path, default=c.OCR_EVAL)
    parser.add_argument("--device", default="cuda", help="cuda (default, fails fast if unavailable) | cpu (explicit, slow)")
    parser.add_argument("--force", action="store_true", help="ignore checkpoints, rerun all repeats")
    parser.add_argument("--output", type=Path, default=c.RESULTS / "ocr_selection.json")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    _info("select-ocr", "args", **vars(args))
    _err_out = args.output
    try:
        if not args.force and args.output.is_file():
            _info("select-ocr", "skipped - already selected (use --force to rerun)", out=str(args.output))
            return

        setup_seed(args.seed)
        device = resolve_device(args.device)
        _info("select-ocr", "device", device=device)
        results: dict = {
            "runs": args.repeats,
            "seed": args.seed,
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
        for model in c.OCR_MODELS:
            stats = {}
            for dataset in c.OCR_DATASETS:
                dataset_dir = args.ocr_eval_dir / dataset
                assets = evaluate.discover_assets(dataset_dir)
                backend = get_ocr(model, device=device, seed=args.seed)
                img_size = c.OCR_IMG_SIZE[model]
                _info("select-ocr", "loading weights (first run downloads GBs)", model=model, dataset=dataset)
                key = f"{model}/{dataset}"
                stored = [list(r) for r in saved_runs.get(key, [])]
                done = len(stored)
                prog = LogProgress(args.repeats, f"select-ocr {model}/{dataset}", unit="repeat", interval_s=30.0)
                prog.n = done
                runs = [_recs(r) for r in stored]
                triples = [list(r) for r in stored]
                for _ in range(done, args.repeats):
                    recs = evaluate.run_ocr(backend, assets, img_size, seed=args.seed)
                    runs.append(recs)
                    triples.append([[rec["cer"], rec["word_accuracy"], rec["latency_ms"]] for rec in recs])
                    saved_runs[key] = triples
                    write_json(partial_path, {"seed": args.seed, "runs": args.repeats, "runs_data": saved_runs})
                    prog.update()
                prog.close()
                summary = evaluate.aggregate_records(runs)
                stats[dataset] = summary
                cer_by_model.setdefault(model, []).extend(rec["cer"] for run in runs for rec in run)
            results["models"][model] = stats
            write_json(_model_file(args.output, model), {"model": model, "seed": args.seed, **stats})

        thai_mean = sum(cer_by_model["ThaiTrOCR"]) / len(cer_by_model["ThaiTrOCR"])
        pp_mean = sum(cer_by_model["PP-OCRv5-mobile"]) / len(cer_by_model["PP-OCRv5-mobile"])
        test = paired_t_test(
            cer_by_model["ThaiTrOCR"], cer_by_model["PP-OCRv5-mobile"], alpha=c.OCR_ALPHA
        )
        results["comparisons"].append(
            {
                "a": "ThaiTrOCR",
                "b": "PP-OCRv5-mobile",
                "paired_t_test": test,
                "cohens_d": cohens_d(cer_by_model["ThaiTrOCR"], cer_by_model["PP-OCRv5-mobile"]),
            }
        )

        decision = (
            "ThaiTrOCR"
            if thai_mean <= c.OCR_CER_THRESHOLD
            else ("ThaiTrOCR" if thai_mean < pp_mean else "PP-OCRv5-mobile")
        )
        results["decision"] = {
            "rule": f"ThaiTrOCR iff mean CER <= {c.OCR_CER_THRESHOLD}, else lowest CER",
            "mean_cer_thaitrocr": thai_mean,
            "mean_cer_ppocrv5": pp_mean,
            "selected": decision,
        }
        write_json(args.output, results)
        partial_path.unlink(missing_ok=True)

        _info("select-ocr", "mean CER", thaitrocr=round(thai_mean, 4), pp_ocrv5=round(pp_mean, 4))
        _info("select-ocr", "paired t-test", p=round(test['p_value'], 4), significant=test['significant'])
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
                _P(_err_path).write_text(json.dumps({"error": str(err), "kind": "select_ocr", "input": str(inp), "output": str(_err_out)}, indent=2))
                _info("select_ocr", "wrote error file", path=_err_path)
            except Exception:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
