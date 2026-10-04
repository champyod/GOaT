#!/usr/bin/env python3
"""Export the models the app ships: PP-OCRv5-mobile -> ONNX, NLLB-200 -> CTranslate2.

src-tauri/src/models.rs resolves both by bare filename, so the names below are
the contract, not a preference (tests/test_export.py pins them against that
file). Nothing counts as exported until the file it claims to have written has
been loaded and run once.
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.log import info as _info
from goat_model.utils import log_call

DEFAULT_DEST = c.MODEL_ROOT / "artifacts"

# Bare names the app searches for (src-tauri/src/models.rs).
OCR_DET_FILE = "ppocrv5_mobile_det.onnx"
OCR_REC_FILE = "ppocrv5_mobile_rec.onnx"
MT_DIR_NAME = "nllb-200-distilled-1.3B-ct2-int8"

# PaddleX official model dirs the two ONNX files are converted from. PaddleOCR
# resolves lang="th" to the Thai PP-OCRv5 recognition sub-model.
OCR_DET_MODEL = "PP-OCRv5_mobile_det"
OCR_REC_MODEL = "th_PP-OCRv5_mobile_rec"

# PaddleX names the static graph and its config after this prefix.
PADDLE_PREFIX = "inference"
# Opset 11 is what PaddleOCR's own deploy tooling targets for the det/rec pair;
# tract is the app's only OCR reader and rejects graphs above it; onnxruntime
# smoke-tests the export at build time, not at runtime.
ONNX_OPSET = 11
CT2_QUANT = "int8"
CT2_WEIGHTS = "model.bin"
SMOKE_SENTENCE = "Hello world."


def _import(name: str, extra: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as err:
        raise RuntimeError(f"{name} not installed - re-run `uv sync {extra}`") from err


def _paddle_source_dir(src: Path, model: str) -> Path:
    """The dir holding the static graph for `model`.

    PaddleX caches one official model per directory, either flattened or under
    an `inference/` subdirectory depending on the version that fetched it.
    """
    for candidate in (src, src / PADDLE_PREFIX):
        graph = candidate / f"{PADDLE_PREFIX}.json"
        params = candidate / f"{PADDLE_PREFIX}.pdiparams"
        if graph.is_file() and params.is_file():
            return candidate
    raise RuntimeError(
        f"{model}: no {PADDLE_PREFIX}.json + {PADDLE_PREFIX}.pdiparams under {src}; "
        f"pass the PaddleX official model dir (default ~/.paddlex/official_models/{model})"
    )


def _paddle2onnx() -> str:
    exe = shutil.which("paddle2onnx")
    if exe is None:
        raise RuntimeError(
            "paddle2onnx not found - PaddleX ships it as an optional plugin: "
            "`uv run paddlex --install paddle2onnx`"
        )
    return exe


def _convert_to_onnx(src: Path, out: Path) -> Path:
    """Same paddle2onnx invocation PaddleX's own CLI makes, minus the config copy."""
    cmd = [
        _paddle2onnx(),
        "--model_dir",
        str(src),
        "--model_filename",
        f"{PADDLE_PREFIX}.json",
        "--params_filename",
        f"{PADDLE_PREFIX}.pdiparams",
        "--save_file",
        str(out),
        "--opset_version",
        str(ONNX_OPSET),
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"paddle2onnx failed for {src} (exit {done.returncode}): {done.stderr.strip()[:400]}")
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"paddle2onnx exited 0 but wrote no {out.name}")
    return out


def _dummy_shape(shape: list) -> tuple[int, int, int, int]:
    """[N,3,H,W] for the smoke run; a dynamic side gets 64 rather than collapsing to 1."""
    dims = [d if isinstance(d, int) and d > 0 else 0 for d in shape]
    n, ch, h, w = (dims + [0, 0, 0, 0])[:4]
    return (n or 1, ch or 3, h or 64, w or 64)


def _onnx_smoke(path: Path) -> None:
    """Load the written file and run one zero batch through it.

    onnxruntime is the only reader in the chain that fails on an unsupported
    opset, so this is what keeps a bad export from reaching the app.
    """
    onnxruntime = _import("onnxruntime", "--extra ocr")
    session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    spec = session.get_inputs()[0]
    session.run(None, {spec.name: np.zeros(_dummy_shape(spec.shape), dtype=np.float32)})


@log_call
def export_onnx(
    det_src: Path | None,
    rec_src: Path | None,
    dest: Path = DEFAULT_DEST,
) -> list[Path]:
    """Convert both PP-OCRv5-mobile sub-models to the two names the app looks up."""
    wanted = ((OCR_DET_MODEL, det_src, OCR_DET_FILE), (OCR_REC_MODEL, rec_src, OCR_REC_FILE))
    plan = []
    for model, src, name in wanted:
        if src is None or not src.is_dir():
            raise RuntimeError(f"{model} source missing - pass its PaddleX official model dir")
        plan.append((_paddle_source_dir(src, model), dest / name))
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for model_dir, out in plan:
        _convert_to_onnx(model_dir, out)
        _onnx_smoke(out)
        _info("export", "exported OCR", file=out.name, bytes=out.stat().st_size)
        written.append(out)
    return written


def _require_mt_src(src: str | Path | None) -> str:
    text = str(src or "").strip()
    if not text:
        raise RuntimeError("MT source missing - pass --mt-src <HF model id or local NLLB dir>")
    local = Path(text).expanduser()
    if not local.is_dir() and (local.exists() or text.startswith(("/", "./", "../", "~"))):
        raise RuntimeError(f"MT source {text} is not a directory")
    return text


def _ct2_smoke(model_dir: Path, tokenizer_src: str) -> None:
    """One real sentence through the converted model, called the way the app calls it."""
    ctranslate2 = _import("ctranslate2", "--extra mt")
    transformers = _import("transformers", "--extra mt")
    tokenizer = transformers.AutoTokenizer.from_pretrained(tokenizer_src, src_lang=c.LANG_CODES["en"])
    translator = ctranslate2.Translator(str(model_dir), device="cpu")
    source = tokenizer.convert_ids_to_tokens(tokenizer.encode(SMOKE_SENTENCE))
    results = translator.translate_batch([source], target_prefix=[[c.LANG_CODES["th"]]])
    # CT2 echoes the forced target-language token; it is a prefix, not output.
    text = tokenizer.decode(
        tokenizer.convert_tokens_to_ids(results[0].hypotheses[0][1:]), skip_special_tokens=True
    )
    if not text.strip():
        raise RuntimeError(f"CTranslate2 smoke translate produced no text from {model_dir.name}")


@log_call
def export_ct2(src: str | Path | None, dest: Path = DEFAULT_DEST) -> Path:
    """Convert NLLB-200 to the INT8 CTranslate2 directory the app loads."""
    source = _require_mt_src(src)
    converters = _import("ctranslate2.converters", "--extra mt")
    out = dest / MT_DIR_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    converters.TransformersConverter(source).convert(
        output_dir=str(out), quantization=CT2_QUANT, force=True
    )
    if not (out / CT2_WEIGHTS).is_file():
        raise RuntimeError(f"conversion reported success but wrote no {out / CT2_WEIGHTS}")
    _ct2_smoke(out, source)
    _info("export", "exported MT", dir=out.name, bytes=(out / CT2_WEIGHTS).stat().st_size)
    return out


@log_call
def main() -> None:
    parser = argparse.ArgumentParser(description="Export the app models (OCR -> ONNX, MT -> CTranslate2).")
    parser.add_argument("--ocr-det-src", type=Path, help=f"PaddleX official model dir for {OCR_DET_MODEL}")
    parser.add_argument("--ocr-rec-src", type=Path, help=f"PaddleX official model dir for {OCR_REC_MODEL}")
    parser.add_argument("--mt-src", help="NLLB HF model id or local model dir to convert")
    parser.add_argument(
        "--dest", type=Path, default=DEFAULT_DEST, help="directory the app reads the models from"
    )
    args = parser.parse_args()

    if not (args.ocr_det_src or args.ocr_rec_src or args.mt_src):
        parser.error("pass at least one of --ocr-det-src / --ocr-rec-src / --mt-src")
    if args.ocr_det_src or args.ocr_rec_src:
        export_onnx(args.ocr_det_src, args.ocr_rec_src, args.dest)
    if args.mt_src:
        export_ct2(args.mt_src, args.dest)


if __name__ == "__main__":
    main()
