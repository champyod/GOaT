"""Integration test: selection produces valid JSON with mocked backends."""

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

# Ensure model/src and notebooks/selection are on sys.path BEFORE any goat_model import
_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1] / "src"  # model/src
SEL = _HERE.parent.parent / "notebooks" / "selection"  # model/notebooks/selection
for p in [str(SRC), str(SEL)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _setup_stubs():
    sb = types.ModuleType("sacrebleu")
    sb.corpus_bleu = lambda h, r: types.SimpleNamespace(
        score=33.0,
        counts=[10, 8, 6, 4],
        totals=[20, 19, 18, 17],
        precisions=[0.5, 0.4, 0.3, 0.2],
        bp=0.9,
    )
    sys.modules["sacrebleu"] = sb
    ft = types.ModuleType("torch")
    ft.cuda = types.SimpleNamespace(is_available=lambda: False)
    ft.Tensor = type("Tensor", (), {})
    sys.modules["torch"] = ft
    sys.modules["transformers"] = types.ModuleType("transformers")
    sys.modules["datasets"] = types.ModuleType("datasets")


def test_select_mt_resume():
    _setup_stubs()
    import goat_model.utils as u

    u.have = lambda *a: False
    import importlib

    import select_mt as sm

    importlib.reload(sm)
    tmp = Path(tempfile.mkdtemp())
    mt = tmp / "mt_test"
    mt.mkdir(parents=True)
    (mt / "flores200.en").write_text("a\nb\n")
    (mt / "flores200.th").write_text("x\ny\n")
    out = tmp / "mt_selection.json"

    def fake_run_mt(backend, sources, refs, batch_size, seed):
        return {"bleu": 30.0, "average_ms_per_sentence": 100.0, "hypotheses": ["h1", "h2"]}

    sys.argv = [
        "x",
        "--mt-test-dir",
        str(mt),
        "--output",
        str(out),
        "--repeats",
        "2",
        "--seed",
        "42",
        "--device",
        "cpu",
    ]
    with (
        mock.patch.object(sm, "get_mt", return_value=object()),
        mock.patch.object(sm, "dataset_revisions", return_value={}),
        mock.patch.object(sm, "_th_tokens", side_effect=lambda texts: [t.split() for t in texts]),
        mock.patch.object(
            sm, "trace_corpus_bleu", return_value={"score": 33.0, "counts": [], "totals": []}
        ),
        mock.patch("goat_model.mt.evaluate.run_mt", side_effect=fake_run_mt),
    ):
        sm.main()
    data = json.loads(out.read_text())
    assert "models" in data and "decision" in data
    assert data["decision"]["selected"] in ["NLLB-200-distilled-600M", "NLLB-200-distilled-1.3B"]


if __name__ == "__main__":
    test_select_mt_resume()
    print("integration smoke ok")
