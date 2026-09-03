#!/usr/bin/env python3
"""RAM monitoring over a GOaT session.

Samples the target process RSS at a fixed rate (default 1 Hz) and reports
the idle mean±std and the active peak. Default target is this process;
pass --pid to monitor the running GOaT app.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model.utils import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="1 Hz RSS sampler for RAM measurement.")
    parser.add_argument("--pid", type=int, default=None, help="process to monitor (default: self)")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to sample")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=Path("results/ram.json"))
    args = parser.parse_args()

    try:
        import psutil
    except ImportError:
        raise SystemExit("psutil not installed - re-run `uv sync`")

    proc = psutil.Process(args.pid) if args.pid else psutil.Process()
    samples: list[float] = []
    deadline = time.monotonic() + args.duration
    print(f"sampling PID {proc.pid} for {args.duration}s @ {args.interval} Hz")
    while time.monotonic() < deadline:
        samples.append(proc.memory_info().rss / 1_000_000)
        time.sleep(args.interval)

    if args.pid:
        import numpy as np

        start = int(len(samples) * 0.2)
        idle = samples[start:]
        summary = {
            "pid": args.pid,
            "idle_mean_mb": float(np.mean(idle)),
            "idle_std_mb": float(np.std(idle, ddof=1)),
            "peak_mb": float(np.max(samples)),
        }
    else:
        import numpy as np

        summary = {
            "pid": proc.pid,
            "all_samples_mb": {
                "mean": float(np.mean(samples)),
                "std": float(np.std(samples, ddof=1)),
                "peak": float(np.max(samples)),
            },
        }
    write_json(args.output, summary)
    print(summary)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
