#!/usr/bin/env python3
"""CPU OCR judge over HF CCYod/Real-GOaT-OCR (500 real desktop screenshots).

One pinned process per model (threads=1, best-effort single-core affinity, no
address-space cap unless --mem-cap-mb asks for one) so latency is comparable;
every model walks the same sorted image list. This file owns the CLI and the
worker lifecycle: one spawned process per model, collected in job order, the
report rewritten as each model lands. Each worker streams its per-image records
to `<output>.samples_<runid>.jsonl` and extends `<output>.partial.json` behind
them, so a run that dies mid-pass is resumed by re-issuing the same command,
and the report is assembled from that log rather than from memory. The report
half - CER / word-accuracy aggregation (metrics only, no WER), paired tests over
the images carrying a non-empty key, and the deferred decision - lives in
goat_model/ocr/judge_report.py. The verdict is deliberately manual: no script
reads the report, so its author applies the OCR_CER_THRESHOLD rule to it.

    uv run python scripts/judge_real_cpu.py --local-dir data/real
    uv run python scripts/judge_real_cpu.py --repeats 3
    uv run python scripts/judge_real_cpu.py --force   # ignore the checkpoint
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import multiprocessing as mp
import os
import queue as queue_mod
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.utils import load_dotenv, log_call, parse_subset_arg, setup_seed, write_json

HF_REPO = "CCYod/Real-GOaT-OCR"
RESULT_TIMEOUT_S = 6 * 3600.0  # one 500-image pass is minutes, not hours
TERMINATE_GRACE_S = 10.0  # SIGTERM -> SIGKILL grace for a worker past its deadline
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
)
EXIT_OK, EXIT_ALL_FAILED, EXIT_INTERRUPTED = 0, 1, 130


def _ocr_module(name: str) -> ModuleType:
    """`goat_model.ocr.<name>` without the package `__init__` side effect.

    goat_model/ocr/__init__.py eagerly imports ocr/train.py, whose module level
    `import torch` would make `--help` fail on a base install. A path-only stub
    keeps real_cpu/real_data/engine/evaluate importable and their frameworks lazy.
    """
    if "goat_model.ocr" not in sys.modules:
        spec = importlib.util.find_spec("goat_model.ocr")
        if spec is None or not spec.submodule_search_locations:
            raise ImportError("goat_model.ocr not importable - run from the model/ checkout")
        package = ModuleType("goat_model.ocr")
        package.__path__ = [str(entry) for entry in spec.submodule_search_locations]
        sys.modules["goat_model.ocr"] = package
    return importlib.import_module(f"goat_model.ocr.{name}")


real_cpu = _ocr_module("real_cpu")
real_data = _ocr_module("real_data")
engine = _ocr_module("engine")
judge_report = _ocr_module("judge_report")
record_log = _ocr_module("record_log")

TAG = real_cpu.TAG


def _parse_args() -> argparse.Namespace:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=fmt)
    parser.add_argument(
        "--local-dir", type=Path, default=c.DATA / "real_hf",
        help=f"{HF_REPO} cache/mirror dir; downloaded into when it holds no images")
    parser.add_argument(
        "--models", default="",
        help=f"comma-separated backend subset; empty = all ({', '.join(engine.BACKENDS)})")
    parser.add_argument(
        "--repeats", type=int, default=1, help="passes per model; >= 2 fills std + the CI")
    parser.add_argument(
        "--mem-cap-mb", type=int, default=None,
        help="opt-in soft RLIMIT_AS per worker in MiB; default = no cap, because torch and "
             "paddle reserve over 1 GiB while loading and a cap would OOM them mid-load")
    parser.add_argument("--seed", type=int, default=c.SEED)
    parser.add_argument(
        "--output", type=Path, default=c.RESULTS / "real500_cpu.json")
    parser.add_argument(
        "--force", action="store_true",
        help="ignore the checkpoint and re-score every image from scratch")
    return parser.parse_args()


def _mem_cap_bytes(args: argparse.Namespace) -> int | None:
    """Per-worker cap in bytes, or None for no cap. Bad input fails loud."""
    if args.repeats < 1:
        raise ValueError(f"--repeats must be >= 1, got {args.repeats}")
    if args.mem_cap_mb is not None and args.mem_cap_mb < 1:
        raise ValueError(f"--mem-cap-mb must be >= 1, got {args.mem_cap_mb}")
    return None if args.mem_cap_mb is None else args.mem_cap_mb * 1024**2


def _assets(root: Path) -> list[real_data.Asset]:
    """The one shared, sorted image list; fetch or reject before workers start."""
    if not real_data.has_images(root):
        if root.is_dir() and any(root.iterdir()):
            raise FileNotFoundError(real_data.unsupported_layout(root))
        real_data.fetch_dataset(HF_REPO, root, os.environ.get("HF_TOKEN"))
        if not real_data.has_images(root):
            raise FileNotFoundError(real_data.unsupported_layout(root))
    return real_data.discover_assets(root)


def _available_cores() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return list(range(os.cpu_count() or 1))


def _record_paths(args: argparse.Namespace, models: list[str]) -> tuple[Path, Path]:
    """This run's checkpoint and the sample log its workers append to.

    The run id is kept in the checkpoint, so a resume lands in the log the dead
    run wrote; --force, or a checkpoint recorded under another seed / repeat
    count, mints a new id and therefore a new log - which is what makes --force
    a re-score of every image instead of an append to a finished run.
    """
    partial = record_log.partial_path(args.output)
    saved = {} if args.force else record_log.load_partial(partial, args.seed, args.repeats)
    if not saved:
        run_id = record_log.new_run_id()
        record_log.start_partial(partial, run_id, args.seed, args.repeats)
        samples = record_log.samples_path(args.output, run_id)
        _info(TAG, "new record log", run_id=run_id, samples=str(samples))
        return partial, samples
    run_id = str(saved["run_id"])
    samples = record_log.samples_path(args.output, run_id)
    for model in models:
        repeats = record_log.partial_stems(partial, model)
        _info(TAG, "resuming", model=model, run_id=run_id, samples=str(samples),
              skipped=sum(len(stems) for stems in repeats.values()))
    return partial, samples


def _build_jobs(models: list[str], assets: list, args: argparse.Namespace,
                cap_bytes: int | None, samples: Path, partial: Path) -> list[real_cpu.Job]:
    cores = _available_cores()
    return [
        real_cpu.Job(
            model=model, assets=assets, img_size=c.OCR_IMG_SIZE[model], seed=args.seed,
            repeats=args.repeats, core=cores[index % len(cores)], mem_cap_bytes=cap_bytes,
            samples_path=samples, partial_path=partial)
        for index, model in enumerate(models)
    ]


def _stop(proc: mp.Process) -> None:
    """SIGTERM, then SIGKILL past the grace period: a worker never outlives us."""
    if proc.is_alive():
        proc.terminate()
        proc.join(TERMINATE_GRACE_S)
    if proc.is_alive():
        proc.kill()
        proc.join()


def _await(job: real_cpu.Job, proc: mp.Process, sink: mp.queues.Queue) -> dict:
    """Collect one worker: a missed deadline or a hard kill surfaces as an error.

    The queue is drained first, then the worker is joined, terminated and finally
    killed, so a silent child can neither hang the parent nor outlive it.
    """
    try:
        result = sink.get(timeout=RESULT_TIMEOUT_S)
    except queue_mod.Empty:
        why = f"no result within {RESULT_TIMEOUT_S:.0f}s (exitcode={proc.exitcode}); terminated"
        _err(TAG, "worker timed out", model=job.model, timeout_s=RESULT_TIMEOUT_S)
        result = real_cpu.outcome(job.model, "error", why, None, None, [], reported=False)
    finally:
        sink.close()
        sink.join_thread()
    proc.join(TERMINATE_GRACE_S)
    _stop(proc)
    if not isinstance(result, dict):
        raise TypeError(f"worker {job.model} returned {type(result).__name__}, expected dict")
    return result


def _harvest(job: real_cpu.Job, proc: mp.Process, sink: mp.queues.Queue,
             outcomes: list[dict]) -> None:
    """Abandon a worker, keeping a result it already delivered.

    Collection runs in job order, so a Ctrl-C while model 1 is still going would
    otherwise throw away what models 2..n had already put on their queues.
    """
    landed = None
    if job.model not in {o["model"] for o in outcomes}:
        try:
            landed = sink.get(timeout=0)
        except (queue_mod.Empty, ValueError):  # nothing delivered, or queue already closed
            landed = None
    sink.close()
    sink.join_thread()
    if isinstance(landed, dict):
        outcomes.append(landed)
        _info(TAG, "recovered delivered result", model=job.model)
    _stop(proc)


def _spawn(jobs: list) -> list[tuple[real_cpu.Job, mp.Process, mp.queues.Queue]]:
    """One spawned worker per model: the judge measures one model per process."""
    ctx = mp.get_context("spawn")
    slots = []
    for job in jobs:
        sink = ctx.Queue()
        proc = ctx.Process(target=real_cpu.worker_main, args=(job, sink), name=job.model)
        proc.start()
        slots.append((job, proc, sink))
    return slots


def run_jobs(jobs: list, outcomes: list[dict], on_progress: Callable[[list[dict]], None]) -> None:
    """Start every worker at once (that is the fairness), then collect in order.

    `outcomes` is caller-owned and `on_progress` rewrites the report per worker.
    """
    for var in THREAD_ENV_VARS:
        os.environ[var] = real_cpu.WORKER_THREADS
    slots = _spawn(jobs)
    _info(TAG, "workers started", models=[job.model for job in jobs], workers=len(slots))
    try:
        for job, proc, sink in slots:
            outcomes.append(_await(job, proc, sink))
            on_progress(outcomes)
    except BaseException:  # a Ctrl-C must not lose a result nor leave a worker running
        for job, proc, sink in slots:
            _harvest(job, proc, sink, outcomes)
        raise


@log_call
def main() -> int:
    args = _parse_args()
    cap_bytes = _mem_cap_bytes(args)
    models = parse_subset_arg(args.models, tuple(engine.BACKENDS), "model")
    load_dotenv()
    setup_seed(args.seed)
    assets = _assets(args.local_dir)
    _info(TAG, "assets", dir=str(args.local_dir), images=len(assets), models=len(models),
          with_key=sum(1 for asset in assets if asset.gt is not None))
    partial, samples = _record_paths(args, models)
    jobs = _build_jobs(models, assets, args, cap_bytes, samples, partial)

    def publish(seen: list[dict]) -> None:
        """One strictly-parseable JSON, rewritten as each model lands."""
        report = judge_report.build_report(jobs, seen, assets, args, samples)
        write_json(args.output, judge_report.json_safe(report))

    outcomes: list[dict] = []
    try:
        run_jobs(jobs, outcomes, publish)
    except KeyboardInterrupt:
        _err(TAG, "interrupted", done=[o["model"] for o in outcomes],
             partial=str(partial), samples=str(samples))
        publish(outcomes)
        return EXIT_INTERRUPTED
    judge_report.log_summary(outcomes)
    failed = [o["model"] for o in outcomes if o["status"] != "ok"]
    if failed:
        # The checkpoint survives: it is what keeps the next run off the images
        # the models that did finish already scored.
        _info(TAG, "incomplete run, checkpoint kept", partial=str(partial), failed=failed)
    else:
        partial.unlink(missing_ok=True)
    _info(TAG, "wrote", out=str(args.output), models=len(outcomes), failed=failed)
    return EXIT_ALL_FAILED if len(failed) == len(outcomes) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
