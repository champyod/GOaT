from __future__ import annotations

import importlib.util
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


def log_call(fn):
    import functools, traceback, os, sys
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        debug = "--debug" in sys.argv or os.environ.get("GOAT_DEBUG") == "1"
        if debug:
            print(f"[enter] {fn.__name__} args={args} kwargs={kwargs}", flush=True)
        else:
            print(f"[enter] {fn.__name__}", flush=True)
        try:
            res = fn(*args, **kwargs)
            if debug:
                print(f"[exit] {fn.__name__} -> {type(res).__name__ if res is not None else 'None'}", flush=True)
            else:
                print(f"[exit] {fn.__name__}", flush=True)
            return res
        except Exception as e:
            print(f"[error] {fn.__name__} {e}", flush=True)
            if debug:
                print(traceback.format_exc(), flush=True)
            raise
    return wrapper


@log_call
def have(*packages: str) -> bool:
    return all(importlib.util.find_spec(pkg) is not None for pkg in packages)


@log_call
def setup_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    if have("torch"):
        import torch

        torch.manual_seed(seed)


@log_call
def read_gt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


@log_call
def read_sentences(path: Path) -> list[str]:
    return [
        line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@log_call
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class LogProgress:
    """TTY-aware progress: tqdm bar on interactive terminals, newline
    heartbeat (every ``interval_s``) in log files where ``\\r`` bars
    would stack into one unreadable line."""

    def __init__(self, total: int, desc: str, unit: str = "it", interval_s: float = 10.0, in_path: str | None = None, out_path: str | None = None) -> None:
        self.total = total
        self.desc = desc
        self.unit = unit
        self.interval = interval_s
        self.in_path = in_path
        self.out_path = out_path
        self.n = 0
        self.t0 = time.monotonic()
        self.last = 0.0
        self.bar = None
        if sys.stdout.isatty():
            from tqdm import tqdm

            self.bar = tqdm(total=total, desc=desc, unit=unit)

    def update(self, k: int = 1) -> None:
        self.n += k
        if self.bar is not None:
            self.bar.update(k)
            return
        now = time.monotonic()
        if now - self.last >= self.interval or self.n >= self.total:
            self.last = now
            el = now - self.t0
            rate = self.n / el if el > 0 else 0.0
            eta = (self.total - self.n) / rate if rate > 0 else -1.0
            suffix = ""
            if self.in_path or self.out_path:
                suffix = f" | in={self.in_path or '?'} out={self.out_path or '?'}"
            print(
                f"[{self.desc}] {self.n}/{self.total} {self.unit} "
                f"elapsed={el:.0f}s eta={eta:.0f}s rate={rate:.1f}/s{suffix}",
                flush=True,
            )

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()


def trainer_heartbeat(desc="train", interval_s=60.0):
    """transformers TrainerCallback printing a living-status heartbeat.

    ``TQDM_DISABLE=1`` kills HF progress bars in batch logs, leaving
    hours-long ``trainer.train()`` silent. This prints
    ``[desc] step n/N epoch=x loss=y elapsed=Ns`` every ``interval_s``.
    transformers is imported lazily so base envs without it still import utils.
    """
    from transformers import TrainerCallback

    class _HeartbeatCallback(TrainerCallback):
        def __init__(self):
            self.t0 = time.monotonic()
            self.last = 0.0

        def on_step_end(self, args, state, control, **kwargs):
            now = time.monotonic()
            if now - self.last < interval_s:
                return control
            self.last = now
            loss = None
            if state.log_history:
                loss = state.log_history[-1].get("loss", state.log_history[-1].get("eval_loss"))
            total = state.max_steps or "?"
            print(
                f"[{desc}] step {state.global_step}/{total} epoch={state.epoch:.2f} "
                f"loss={loss} elapsed={now - self.t0:.0f}s",
                flush=True,
            )
            return control

    return _HeartbeatCallback()


def copy_replace(src: Path, dst: Path) -> None:
    """copy2 that survives Drive FUSE: overwriting an existing Drive file in
    place can raise PermissionError, while delete-then-write succeeds.
    Deliberately undecorated: called per file in 10k-copy loops."""
    import shutil

    try:
        shutil.copy2(src, dst)
    except PermissionError:
        try:
            dst.unlink()
        except OSError:
            pass
        shutil.copy2(src, dst)


def resolve_device(requested: str = "cuda") -> str:
    """Single device policy: CUDA everywhere, never silent CPU.

    ``"cuda"`` (default) raises RuntimeError when CUDA is unavailable instead
    of falling back. ``"cpu"`` is allowed only when explicitly requested, with
    a loud warning. Anything else is treated as CUDA (fail fast).
    """
    if requested == "cpu":
        print("[warn] resolve_device: explicit CPU - expect hours-long runs", flush=True)
        return "cpu"
    if not have("torch"):
        raise RuntimeError("torch not installed - cannot verify CUDA; run `uv sync --extra mt`")
    import torch

    if torch.cuda.is_available():
        return "cuda"
    raise RuntimeError(
        f"device {requested!r} requested but CUDA unavailable - fix torch/CUDA "
        "(never fall back to CPU silently)"
    )
