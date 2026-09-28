"""Parallel selection: subset parsing + worker-merge verdicts.

merge_selection.py is loaded by path (scripts/ is not a package); it needs
only stdlib + constants + utils.write_json, so no framework stubs here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODEL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODEL_ROOT / "src"))


def _load_merge():
    spec = importlib.util.spec_from_file_location(
        "merge_selection", MODEL_ROOT / "scripts" / "merge_selection.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge_selection = _load_merge()

from goat_model.utils import parse_subset_arg  # noqa: E402


def _ocr_worker(model: str, means: dict[str, tuple[int, float]], seed: int = 42) -> dict:
    return {
        "runs": 2,
        "seed": seed,
        "models": {
            model: {
                ds: {"cer": {"mean": mean}, "n_images": n, "n_runs": 2}
                for ds, (n, mean) in means.items()
            }
        },
        "comparisons": [],
        "decision": {"selected": None},
    }


def test_parse_subset_defaults_dedupes_and_rejects() -> None:
    assert parse_subset_arg("", ("a", "b"), "model") == ["a", "b"]
    assert parse_subset_arg("b,a,b", ("a", "b"), "model") == ["b", "a"]
    with pytest.raises(ValueError, match="unknown model"):
        parse_subset_arg("a,z", ("a", "b"), "model")


def test_merge_ocr_weighted_mean_is_exact(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_ocr_worker("A", {"d1": (100, 0.8), "d2": (300, 0.4)})))
    b.write_text(json.dumps(_ocr_worker("B", {"d1": (100, 0.6), "d2": (300, 0.6)})))
    out = tmp_path / "merged.json"
    merged = merge_selection.merge_files("ocr", [a, b], out)
    assert merged["decision"]["mean_cer"]["A"] == pytest.approx(0.5)
    assert merged["decision"]["mean_cer"]["B"] == pytest.approx(0.6)
    assert merged["decision"]["selected"] == "A"
    assert json.loads(out.read_text())["decision"]["selected"] == "A"


def test_merge_mt_applies_gate(tmp_path: Path) -> None:
    def worker(model: str, bleu: float, lat: float) -> Path:
        p = tmp_path / f"{model}.json"
        p.write_text(
            json.dumps(
                {
                    "runs": 2,
                    "seed": 42,
                    "models": {
                        model: {"bleu": {"mean": bleu}, "avg_s_per_sentence": {"mean": lat}}
                    },
                    "comparisons": [],
                    "decision": {"selected": None},
                }
            )
        )
        return p

    m600 = "NLLB-200-distilled-600M"
    m13 = "NLLB-200-distilled-1.3B"
    out = tmp_path / "merged.json"
    merged = merge_selection.merge_files(
        "mt", [worker(m600, 40.0, 1.0), worker(m13, 30.0, 3.0)], out
    )
    assert merged["decision"]["selected"] == m600
    merged = merge_selection.merge_files(
        "mt", [worker(m600, 10.0, 1.0), worker(m13, 30.0, 3.0)], out
    )
    assert merged["decision"]["selected"] == m13


def test_merge_rejects_mismatched_workers(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_ocr_worker("A", {"d1": (10, 0.5)})))
    b.write_text(json.dumps(_ocr_worker("B", {"d1": (10, 0.5)}, seed=7)))
    with pytest.raises(SystemExit, match="disagree"):
        merge_selection.merge_files("ocr", [a, b], tmp_path / "out.json")
    c = tmp_path / "c.json"
    c.write_text(json.dumps(_ocr_worker("A", {"d1": (10, 0.5)})))
    with pytest.raises(SystemExit, match="two workers"):
        merge_selection.merge_files("ocr", [a, c], tmp_path / "out.json")
