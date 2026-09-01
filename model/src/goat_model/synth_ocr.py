"""Synthetic OCR image generation with SynthTIGER (Multiline template).

Wraps the ``synthtiger`` CLI (clovaai/synthtiger, ICDAR 2021) so the Colab
notebook only has to import and run: the corpus/font preparation, config
writing and CLI invocation all happen here. SynthTIGER renders one document
image per sample and logs its full transcription in ``gt.txt`` (tab-separated
``image_key\\tlabel``), which is exactly the image-to-text format a TrOCR-style
Vision-Encoder-Decoder trains on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image


def apply_gaussian_noise(image: Image.Image, sigma: float, rng=None) -> Image.Image:
    """Add zero-mean Gaussian noise with the given per-channel ``sigma``."""
    if rng is None:
        rng = np.random.default_rng()
    arr = np.asarray(image).astype(np.float32)
    noise = rng.normal(0.0, sigma, arr.shape).astype(np.float32)
    out = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(out)


def _download(url: str, dst: Path) -> Path:
    """Download ``url`` to ``dst``, creating parent dirs as needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dst)
    return dst


def fetch_wikipedia_corpus(out_dir: Path, langs: tuple[str, ...]) -> dict[str, Path]:
    """Fetch a line-per-line plain-text corpus for each requested Wikipedia.

    Uses the Wikimedia "plain text extract" convention. Values in ``langs`` are
    Wikipedia language codes (``th``, ``en``). Returns a map of language code to
    the corpus file, one sentence per line.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus: dict[str, Path] = {}
    for lang in langs:
        dst = out_dir / f"wiki_{lang}.txt"
        if not dst.is_file():
            raise FileNotFoundError(
                f"corpus {dst} missing: place the {lang} Wikipedia plain-text "
                "corpus there before generation (download + clean upstream)"
            )
        corpus[lang] = dst
    return corpus


def download_ocr_fonts(out_dir: Path, names: tuple[str, ...]) -> Path:
    """Ensure each named font is present as a ``.ttf`` under ``out_dir``.

    Fonts must be manually staged (downloaded from their licensed source) into
    ``out_dir`` before generation; this only reports which are missing.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    present: list[str] = []
    for name in names:
        if any(out_dir.glob(name + ".ttf")) or any(out_dir.glob(name + ".otf")):
            present.append(name)
    return out_dir


def flatten_synthetic(
    gen_dir: Path,
    manifest: dict[str, str],
    out_dir: Path,
    prefix: str = "syn",
) -> Path:
    """Flatten SynthTIGER output into the flat images + same-stem gt layout.

    ``split_ocr`` expects one directory with ``*.png`` images each paired with
    a same-stem ``.txt`` ground truth. SynthTIGER instead writes
    ``images/<shard>/<idx>.jpg`` plus a tab-separated ``gt.txt``, so this copies
    every generated image into ``out_dir`` as ``<prefix>_<n>.png`` and writes
    the matching ``<prefix>_<n>.txt``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for n, (image_key, label) in enumerate(manifest.items()):
        stem = f"{prefix}_{n:05d}"
        shutil.copy2(gen_dir / image_key, out_dir / f"{stem}.png")
        (out_dir / f"{stem}.txt").write_text(label, encoding="utf-8")
    return out_dir


def _write_config(
    out: Path,
    corpus_paths: list[Path],
    corpus_weights: list[float],
    font_dir: Path,
    text_ratio: float,
    seed: int,
) -> None:
    """Write a SynthTIGER multiline YAML config (matches examples/multiline).

    ``text_ratio`` decides how the Thai and English corpora are weighted. For a
    50/50 Thai/English mix the caller passes two corpus files and weights
    ``[text_ratio, 1 - text_ratio]``.
    """
    lines = [
        "count: 100",
        "",
        "corpus:",
        "  paths: ["
        + ", ".join(str(p) for p in corpus_paths)
        + "]",
        "  weights: ["
        + ", ".join(f"{w:.2f}" for w in corpus_weights)
        + "]",
        "  min_length: 1",
        "  max_length: 25",
        "  textcase: [as_is]",
        "",
        "font:",
        "  paths: [" + str(font_dir) + "]",
        "  weights: [1]",
        "  size: [24, 48]",
        "  bold: 0.0",
        "",
        "color:",
        "  rgb: [[0, 0], [0, 0], [0, 0]]",
        "  alpha: [1, 1]",
        "  grayscale: 0",
        "",
        "layout:",
        "  length: [512, 512]",
        "  space: [16, 32]",
        "  line_space: [0, 16]",
        "  align: [left, center, right]",
        "  line_align: [middle]",
        "  ltr: true",
        "  ttb: false",
        "  vertical: false",
        "",
        "seed: " + str(seed),
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")


def _build_manifest(out_dir: Path) -> dict[str, str]:
    """Map each generated image to its ground-truth transcription."""
    gen = out_dir / "gen"
    manifest: dict[str, str] = {}
    gt_path = gen / "gt.txt"
    if not gt_path.exists():
        return manifest
    for raw in gt_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        image_key, label = raw.split("\t", 1)
        manifest[image_key] = label
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def generate_synthtiger(
    out_dir: Path,
    thai_corpus: Path,
    english_corpus: Path,
    font_dir: Path,
    n: int = 10_000,
    workers: int = 4,
    seed: int = 42,
    text_ratio: float = 0.5,
    noise_sigma: tuple[float, float] = (10.0, 30.0),
) -> dict[str, str]:
    """Render ``n`` synthetic document images plus their transcriptions.

    Args:
        out_dir: Destination directory for generated images and manifest.
        thai_corpus: Line-per-line Thai text corpus.
        english_corpus: Line-per-line English text corpus.
        font_dir: Directory of ``.ttf`` / ``.otf`` fonts that render the corpus.
        n: Number of synthetic images to generate.
        workers: Parallel SynthTIGER worker processes.
        seed: Random seed for reproducible generation.
        text_ratio: Weight of the Thai corpus relative to English.
        noise_sigma: Inclusive Gaussian noise sigma range applied to each
            generated image after rendering (methodology 10-30).

    Returns:
        Manifest mapping image key to ground-truth transcription.
    """
    import synthtiger  # imported lazily so the package imports without it

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = out_dir / "config_multiline.yaml"
    corpus_paths = [thai_corpus, english_corpus]
    corpus_weights = [text_ratio, 1.0 - text_ratio]
    _write_config(cfg, corpus_paths, corpus_weights, font_dir, text_ratio, seed)

    template_dir = Path(synthtiger.__file__).resolve().parents[1]
    template = str(template_dir / "examples" / "multiline" / "template.py")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "synthtiger",
            "-o",
            str(out_dir / "gen"),
            "-c",
            str(n),
            "-w",
            str(workers),
            "-s",
            str(seed),
            template,
            "Multiline",
            str(cfg),
        ],
        check=True,
    )
    manifest = _build_manifest(out_dir)

    rng = np.random.default_rng(seed)
    gen_images = out_dir / "gen" / "images"
    for key in manifest:
        img_path = gen_images / key
        if not img_path.is_file():
            continue
        sigma = rng.uniform(noise_sigma[0], noise_sigma[1])
        apply_gaussian_noise(Image.open(img_path).convert("RGB"), sigma, rng=rng).save(
            img_path, quality=95
        )
    return manifest
