"""Export + CTranslate2 backend guards. No weights, no network, no GPU.

The two properties worth pinning are that the exporters fail loudly instead of
writing nothing and calling it a success, and that the names they write are the
ones the app looks up.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]  # model/
_SCRIPTS = _ROOT / "scripts"
_APP = _ROOT.parent / "src-tauri" / "src" / "models.rs"


def _export() -> types.ModuleType:
    """Import the exporter by path, mirroring how the scripts are run."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import export_models

    return export_models


def _engine() -> types.ModuleType:
    import goat_model.mt.engine

    return goat_model.mt.engine


def _paddle_source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "inference.json").write_text("{}", encoding="utf-8")
    (root / "inference.pdiparams").write_bytes(b"\x00")
    return root


def test_export_onnx_refuses_a_missing_source(tmp_path: Path) -> None:
    ex = _export()
    with pytest.raises(RuntimeError, match="source missing"):
        ex.export_onnx(None, None, tmp_path)
    with pytest.raises(RuntimeError, match="source missing"):
        ex.export_onnx(tmp_path / "absent", None, tmp_path)


def test_export_onnx_refuses_a_dir_without_a_paddle_graph(tmp_path: Path) -> None:
    ex = _export()
    empty = tmp_path / "PP-OCRv5_mobile_det"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="inference.json"):
        ex.export_onnx(empty, None, tmp_path)


def test_export_onnx_fails_when_the_converter_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    """No paddle2onnx means no ONNX. It must say so, not write a stub file."""
    ex = _export()
    det = _paddle_source(tmp_path / "PP-OCRv5_mobile_det")
    rec = _paddle_source(tmp_path / "th_PP-OCRv5_mobile_rec")
    monkeypatch.setattr(ex.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="paddle2onnx not found"):
        ex.export_onnx(det, rec, tmp_path)
    assert not (tmp_path / ex.OCR_DET_FILE).exists()
    assert not (tmp_path / ex.OCR_REC_FILE).exists()


def test_export_onnx_fails_when_the_converter_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    ex = _export()
    det = _paddle_source(tmp_path / "PP-OCRv5_mobile_det")
    rec = _paddle_source(tmp_path / "th_PP-OCRv5_mobile_rec")
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/usr/bin/paddle2onnx")
    monkeypatch.setattr(
        ex.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
    )
    with pytest.raises(RuntimeError, match="wrote no"):
        ex.export_onnx(det, rec, tmp_path)


def test_export_onnx_never_reports_success_on_a_converter_failure(tmp_path: Path, monkeypatch) -> None:
    ex = _export()
    det = _paddle_source(tmp_path / "PP-OCRv5_mobile_det")
    rec = _paddle_source(tmp_path / "th_PP-OCRv5_mobile_rec")
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/usr/bin/paddle2onnx")
    monkeypatch.setattr(
        ex.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "opset 11 unsupported"),
    )
    with pytest.raises(RuntimeError, match="opset 11 unsupported"):
        ex.export_onnx(det, rec, tmp_path)


def test_exported_names_are_the_ones_the_app_looks_up() -> None:
    ex = _export()
    rust = _APP.read_text(encoding="utf-8")
    assert f'OCR_DETECTION_MODEL: &str = "{ex.OCR_DET_FILE}"' in rust
    assert f'OCR_RECOGNITION_MODEL: &str = "{ex.OCR_REC_FILE}"' in rust
    assert f'NLLB_MODEL_DIR: &str = "{ex.MT_DIR_NAME}"' in rust


def test_export_ct2_refuses_a_missing_source(tmp_path: Path) -> None:
    ex = _export()
    with pytest.raises(RuntimeError, match="MT source missing"):
        ex.export_ct2(None, tmp_path)
    with pytest.raises(RuntimeError, match="not a directory"):
        ex.export_ct2(tmp_path / "absent", tmp_path)


def test_export_ct2_names_the_missing_dependency(tmp_path: Path) -> None:
    ex = _export()
    src = _paddle_source(tmp_path / "nllb")
    with pytest.raises(RuntimeError, match="ctranslate2.converters not installed"):
        ex.export_ct2(src, tmp_path)


class _FakeOptions:
    def __init__(self, **kw) -> None:
        self.kw = kw


class _FakeResult:
    def __init__(self, hypothesis: list[str]) -> None:
        self.hypotheses = [hypothesis]


_CALLS: list[dict] = []


class _FakeTranslator:
    """Stands in for ctranslate2.Translator; records how it was called."""

    def __init__(self, model_path: str, device: str = "cpu", **kw) -> None:
        self.model_path = model_path
        self.device = device

    def translate_batch(self, source, target_prefix=None, options=None, **kw):
        _CALLS.append({"source": list(source), "target_prefix": target_prefix, "options": options})
        return [_FakeResult([target_prefix[0][0], "▁สวัสดี", "▁ชาวโลก"]) for _ in source]


class _FakeAutoTokenizer:
    @classmethod
    def from_pretrained(cls, name: str, **kw) -> _FakeAutoTokenizer:
        return cls()

    def encode(self, text: str) -> list[int]:
        return [1, 2, 3]

    def convert_ids_to_tokens(self, ids) -> list[str]:
        return [f"tok{i}" for i in ids]

    def convert_tokens_to_ids(self, tokens) -> list[int]:
        return list(range(len(tokens)))

    def decode(self, ids, skip_special_tokens: bool = False) -> str:
        return "สวัสดีชาวโลก" if skip_special_tokens else "tha_Thai สวัสดีชาวโลก"

    def __call__(self, texts, add_special_tokens: bool = True) -> dict:
        assert add_special_tokens is False, "n_tokens must exclude special tokens"
        return {"input_ids": [[1, 2] for _ in texts]}


@pytest.fixture
def fake_ct2(monkeypatch) -> None:
    """Swap both heavy imports for doubles so the protocol is testable offline."""
    ct2 = types.ModuleType("ctranslate2")
    ct2.Translator = _FakeTranslator
    ct2.TranslationOptions = _FakeOptions
    converters = types.ModuleType("ctranslate2.converters")
    converters.TransformersConverter = _FakeAutoTokenizer
    ct2.converters = converters
    hf = types.ModuleType("transformers")
    hf.AutoTokenizer = _FakeAutoTokenizer
    monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
    monkeypatch.setitem(sys.modules, "ctranslate2.converters", converters)
    monkeypatch.setitem(sys.modules, "transformers", hf)
    _CALLS.clear()


def test_ct2_backend_satisfies_the_mt_backend_protocol(tmp_path: Path, fake_ct2) -> None:
    engine = _engine()
    backend = engine.NLLBCTranslate2(
        model_path=tmp_path,
        tokenizer_id="facebook/nllb-200-distilled-1.3B",
        src_lang="eng_Latn",
        tgt_lang="tha_Thai",
        device="cpu",
    )
    assert isinstance(backend, engine.MTBackend)

    result = backend.translate(["Hello world.", "Second sentence."])
    assert result.translations == ["สวัสดีชาวโลก", "สวัสดีชาวโลก"]
    assert result.latency_ms > 0.0
    assert backend.n_tokens(["one two", "three"]) == 4

    call = _CALLS[0]
    assert call["target_prefix"] == [["tha_Thai"], ["tha_Thai"]]
    assert call["options"].kw == {
        "beam_size": 4,
        "max_decoding_length": 256,
        "length_penalty": 1.0,
    }


def test_ct2_backend_translates_nothing_without_loading(tmp_path: Path, fake_ct2) -> None:
    engine = _engine()
    backend = engine.NLLBCTranslate2(
        model_path=tmp_path,
        tokenizer_id="facebook/nllb-200-distilled-1.3B",
        src_lang="eng_Latn",
        tgt_lang="tha_Thai",
        device="cpu",
    )
    result = backend.translate([])
    assert result.translations == [] and result.latency_ms == 0.0
    assert _CALLS == []


def test_ct2_backend_refuses_a_missing_model_dir(tmp_path: Path) -> None:
    engine = _engine()
    with pytest.raises(RuntimeError, match="not found"):
        engine.NLLBCTranslate2(
            model_path=tmp_path / "absent",
            tokenizer_id="facebook/nllb-200-distilled-1.3B",
            src_lang="eng_Latn",
            tgt_lang="tha_Thai",
            device="cpu",
        )


def test_get_mt_selects_the_ct2_backend_for_a_converted_path(tmp_path: Path) -> None:
    engine = _engine()
    backend = engine.get_mt(
        model_name="NLLB-200-distilled-1.3B",
        src_lang="eng_Latn",
        tgt_lang="tha_Thai",
        device="cpu",
        model_path=tmp_path,
    )
    assert isinstance(backend, engine.NLLBCTranslate2)
    assert backend.tokenizer_id == engine.NLLB_HF_IDS["NLLB-200-distilled-1.3B"]


def test_get_mt_keeps_the_transformers_backend_as_the_default() -> None:
    engine = _engine()
    with pytest.raises(ValueError, match="unknown MT model"):
        engine.get_mt(model_name="not-a-model", src_lang="eng_Latn", tgt_lang="tha_Thai", device="cpu")
