"""Unit tests: synth preflight fails fast on unusable fonts/corpus.

Covers ``_preflight_local`` in ``goat_model.synth_ocr``: a zero-font or
zero-text config must raise here with a clear message instead of hanging
``0/N`` inside SynthTIGER's silent retry loop.
"""
import sys
import types
from pathlib import Path

# Ensure model/src is on sys.path BEFORE any goat_model import
_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1] / "src"  # model/src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _install_fake_components(corpus_ok=True, font_ok=True):
    """Stub ``synthtiger.components`` with controllable fakes."""
    class FakeCorpus:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sample(self):
            if not corpus_ok:
                raise RuntimeError("There is no text: /fake/wiki.txt")
            return {"text": "test"}

        def data(self, meta):
            return meta["text"]

    class FakeFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sample(self):
            if not font_ok:
                raise RuntimeError("There is no font: /fake/fonts")
            return {"path": "/fake/fonts/a.ttf", "size": 32, "bold": False, "vertical": False}

    pkg = types.ModuleType("synthtiger")
    comps = types.ModuleType("synthtiger.components")
    setattr(comps, "BaseCorpus", FakeCorpus)
    setattr(comps, "BaseFont", FakeFont)
    setattr(pkg, "components", comps)
    sys.modules["synthtiger"] = pkg
    sys.modules["synthtiger.components"] = comps


def _preflight():
    import goat_model.synth_ocr as so
    import importlib
    importlib.reload(so)
    return so._preflight_local


def test_preflight_passes_on_healthy_inputs():
    _install_fake_components(corpus_ok=True, font_ok=True)
    preflight = _preflight()
    preflight([Path("/fake/wiki_th.txt")], [1.0], Path("/fake/fonts"))


def test_preflight_raises_on_zero_fonts():
    _install_fake_components(corpus_ok=True, font_ok=False)
    preflight = _preflight()
    try:
        preflight([Path("/fake/wiki_th.txt")], [1.0], Path("/fake/fonts"))
    except RuntimeError as err:
        assert "font" in str(err).lower()
    else:
        raise AssertionError("expected RuntimeError for zero-font config")


def test_preflight_raises_on_zero_text():
    _install_fake_components(corpus_ok=False, font_ok=True)
    preflight = _preflight()
    try:
        preflight([Path("/fake/wiki_th.txt")], [1.0], Path("/fake/fonts"))
    except RuntimeError as err:
        assert "text" in str(err).lower()
    else:
        raise AssertionError("expected RuntimeError for zero-text config")


if __name__ == "__main__":
    test_preflight_passes_on_healthy_inputs()
    test_preflight_raises_on_zero_fonts()
    test_preflight_raises_on_zero_text()
    print("preflight smoke ok")
