from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path
from typing import Any

import numpy as np


def have(*packages: str) -> bool:
    return all(importlib.util.find_spec(pkg) is not None for pkg in packages)


def setup_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    if have("torch"):
        import torch

        torch.manual_seed(seed)


def read_gt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def read_sentences(path: Path) -> list[str]:
    return [
        line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
