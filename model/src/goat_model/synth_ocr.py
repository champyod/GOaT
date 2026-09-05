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
from goat_model.constants import MODEL_ROOT
from goat_model.utils import log_call, LogProgress

#: Watchdog bound (seconds) for the SynthTIGER subprocess. A silent
#: per-sample retry-loop hang becomes a fast ``TimeoutExpired`` instead
#: of a ``0/N`` stall with no error.
SYNTH_GEN_TIMEOUT_S = 7200.0


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



def _urlopen_json(req, timeout: int = 30, tries: int = 6):
    """GET ``req`` as JSON with exponential backoff on HTTP 429."""
    import time
    import urllib.error

    delay = 5.0
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code != 429 or attempt == tries - 1:
                raise
            retry_after = err.headers.get("Retry-After") if err.headers else None
            wait = float(retry_after) if retry_after else delay
            print(f"wikipedia 429 - retry in {wait:.0f}s (attempt {attempt + 1}/{tries})", flush=True)
            time.sleep(wait)
            delay *= 2
    raise SystemExit("wikipedia kept returning 429 - rerun later to resume")


@log_call
def fetch_wikipedia_corpus(
    out_dir: Path,
    langs: tuple[str, ...],
    n_lines: int = 20_000,
) -> dict[str, Path]:
    """Download a line-per-line plain-text corpus for each requested Wikipedia.

    Queries the MediaWiki API for random pages with ``prop=extracts`` and
    ``explaintext``, which returns clean plain text; section headers and empty
    lines are dropped and every remaining line is written to ``wiki_{lang}.txt``.
    Values in ``langs`` are Wikipedia language codes (``th``, ``en``). Skips a
    language when its corpus file already exists; returns a map of language code
    to the corpus file, one sentence per line.
    """
    import json
    import re
    import time
    import urllib.parse

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus: dict[str, Path] = {}
    for lang in langs:
        dst = out_dir / f"wiki_{lang}.txt"
        if dst.is_file():
            corpus[lang] = dst
            continue
        part = out_dir / f"wiki_{lang}.partial.txt"
        seen: set[str] = set()
        lines: list[str] = []
        if part.is_file():
            for raw in part.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
            if lines:
                print(f"[wiki-{lang}] resuming {len(lines)}/{n_lines} lines from {part.name}", flush=True)
        query = (
            "action=query&format=json&generator=random&grnnamespace=0"
            "&grnlimit=10&prop=extracts&explaintext=1"
        )
        url = f"https://{lang}.wikipedia.org/w/api.php?" + query
        requests = 0
        prog = LogProgress(n_lines, f"wiki-{lang}", unit="lines", interval_s=5.0, in_path=f"wiki {lang} API", out_path=str(dst))
        prog.n = len(lines)
        while len(lines) < n_lines and requests < 2000:
            requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": "GOaT/1.0"})
            data = _urlopen_json(req, timeout=30)
            time.sleep(0.5)
            batch_new: list[str] = []
            for page in data["query"]["pages"].values():
                text = page.get("extract", "")
                for raw in text.splitlines():
                    line = raw.strip()
                    if len(line) < 4 or re.match(r"^=+\s", line) or line in seen:
                        continue
                    seen.add(line)
                    lines.append(line)
                    batch_new.append(line)
                    if len(lines) >= n_lines:
                        break
            if batch_new:
                with part.open("a", encoding="utf-8") as fh:
                    fh.write("".join(ln + "\n" for ln in batch_new))
                prog.update(len(batch_new))
        prog.close()
        print(f"[wiki-{lang}] done {len(lines[:n_lines])}/{n_lines} -> {dst.name} (src wiki API)", flush=True)
        dst.write_text("\n".join(lines[:n_lines]) + "\n", encoding="utf-8")
        part.unlink(missing_ok=True)
        corpus[lang] = dst
    return corpus


@log_call
def download_ocr_fonts(out_dir: Path, names: tuple[str, ...]) -> Path:
    """Fetch each named font family as ``.ttf`` under ``out_dir``.

    Downloads the Regular (and Bold when the family ships one) TTF from the
    google/fonts GitHub repository into ``out_dir`` as
    ``<family>-<Weight>.ttf``, skipping a family already present. Uses only the
    standard library (``urllib`` and ``urllib.parse.quote`` for bracket names).
    """
    import urllib.parse

    base = "https://raw.githubusercontent.com/google/fonts/main/"
    sources: dict[str, tuple[str, tuple[str, ...]]] = {
        "TH Sarabun PSK": ("ofl/sarabun", ("Sarabun-Regular.ttf", "Sarabun-Bold.ttf")),
        "Noto Sans Thai": ("ofl/notosansthai", ("NotoSansThai[wdth,wght].ttf",)),
        "Kanit": ("ofl/kanit", ("Kanit-Regular.ttf", "Kanit-Bold.ttf")),
        "Prompt": ("ofl/prompt", ("Prompt-Regular.ttf", "Prompt-Bold.ttf")),
        "Sriracha": ("ofl/sriracha", ("Sriracha-Regular.ttf",)),
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from goat_model.utils import log_call, LogProgress as _LP
    fonts_prog = _LP(len(names), "fonts", unit="fonts", interval_s=5.0)
    for name in names:
        if name not in sources:
            raise ValueError(f"no google/fonts source mapped for {name!r}")
        if any(out_dir.glob(name + "*")):
            fonts_prog.update()
            continue
        repo_dir, repo_files = sources[name]
        for repo_file in repo_files:
            weight = "Bold" if "Bold" in repo_file else "Regular"
            dst = out_dir / f"{name}-{weight}.ttf"
            url = base + repo_dir + "/" + urllib.parse.quote(repo_file)
            _download(url, dst)
        fonts_prog.update()
    fonts_prog.close()
    print(f"[fonts] {len(list(out_dir.glob('*.ttf')))} ttf -> {out_dir}", flush=True)
    return out_dir


@log_call
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


@log_call
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
        image_key = image_key.replace("\\", "/")
        manifest[image_key] = label
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _preflight_local(
    corpus_paths: list[Path],
    corpus_weights: list[float],
    font_dir: Path,
    probes: int = 5,
) -> None:
    """Fail fast when workers would spin forever on unusable inputs.

    Mirrors worker startup (template ``__init__``) plus per-sample
    ``corpus.sample()`` / ``font.sample()`` so a zero-font or zero-text
    config raises here with a clear message instead of hanging ``0/N``
    inside SynthTIGER's silent retry loop.
    """
    from synthtiger import components

    corpus = components.BaseCorpus(
        paths=[str(p) for p in corpus_paths],
        weights=list(corpus_weights),
        min_length=1,
        max_length=25,
    )
    for _ in range(probes):
        corpus.data(corpus.sample())
    font = components.BaseFont(
        paths=[str(font_dir)],
        weights=[1],
        size=[24, 48],
        bold=0.0,
    )
    for _ in range(probes):
        font.sample()


@log_call
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
    debug: bool = False,
    timeout_s: float = SYNTH_GEN_TIMEOUT_S,
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
        debug: Forward ``-v`` to the SynthTIGER subprocess so swallowed
            per-sample tracebacks land in ``synth_gen.log``.
        timeout_s: Watchdog bound on the SynthTIGER subprocess. A silent
            retry-loop hang becomes a fast ``TimeoutExpired`` instead.

    Returns:
        Manifest mapping image key to ground-truth transcription.
    """
    import synthtiger  # imported lazily so the package imports without it

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    drive_gen = out_dir / "gen"
    local_gen = Path("/tmp/synth_gen")
    # resume source = Drive (persist), but local wins if exists (VM speed)
    # sync Drive -> local first if local empty
    if drive_gen.is_dir() and not any(local_gen.rglob("*.jpg")):
        import shutil as _sh
        try:
            if local_gen.exists():
                _sh.rmtree(local_gen)
            _sh.copytree(drive_gen, local_gen)
            print(f"[synth-gen] copied {drive_gen} -> {local_gen} for fast resume", flush=True)
        except Exception as e:
            print(f"[synth-gen] copy failed {e}, fresh local", flush=True)
            local_gen.mkdir(parents=True, exist_ok=True)
    else:
        local_gen.mkdir(parents=True, exist_ok=True)
    gen_root = local_gen
    existing = sum(1 for _ in gen_root.rglob("*.jpg")) if gen_root.is_dir() else 0
    # also count Drive if local empty but Drive has (fallback)
    if existing == 0 and drive_gen.is_dir():
        existing = sum(1 for _ in drive_gen.rglob("*.jpg"))
        if existing > 0:
            print(f"[synth-gen] drive has {existing}, local empty - will resume from drive count", flush=True)
    if existing >= n:
        print(f"[synth-gen] skip {existing}/{n} already exists -> {drive_gen}", flush=True)
        return _build_manifest(out_dir)
    if existing > 0:
        seed = seed + existing
        n = n - existing
        print(f"[synth-gen] resume {existing} existing, generating {n} remaining (seed offset) local={gen_root}", flush=True)
    vendored = MODEL_ROOT / "assets" / "synthtiger_templates" / "multiline" / "template.py"
    if vendored.is_file():
        template = str(vendored)
    else:
        pkg_dir = Path(synthtiger.__file__).resolve().parent
        template = str(pkg_dir / "examples" / "multiline" / "template.py")
    if not Path(template).is_file():
        raise SystemExit(f"synthtiger template missing: {template}")

    import threading

    stop_evt = threading.Event()
    gen_root = local_gen  # count where the subprocess -o writes, not Drive
    prog = LogProgress(n, "synth-gen", unit="imgs", interval_s=10.0, in_path=str(thai_corpus), out_path=str(out_dir))
    def _watch():
        while not stop_evt.wait(10.0):
            try:
                cnt = sum(1 for _ in gen_root.rglob("*.jpg"))
                cnt += sum(1 for _ in gen_root.rglob("*.png"))
            except Exception:
                cnt = 0
            # problem logger: head + tail of synthtiger log if stuck at 0
            if cnt == 0:
                try:
                    lines = Path(synth_log).read_text().splitlines()
                    if lines:
                        print(f"[synth-gen] log head: {' | '.join(lines[:3])}", flush=True)
                        print(f"[synth-gen] log tail: {' | '.join(lines[-2:])}", flush=True)
                except Exception:
                    pass
            prog.update(max(0, cnt - prog.n))

    th = threading.Thread(target=_watch, daemon=True)
    th.start()
    # Local copy fonts + corpus for speed (Drive -> /tmp, local wins)
    import shutil as _shf
    local_font_dir = Path("/tmp/synth_fonts")
    local_corpus_dir = Path("/tmp/synth_corpus")
    try:
        if not local_font_dir.is_dir() or not any(local_font_dir.glob("*.ttf")):
            if local_font_dir.exists():
                _shf.rmtree(local_font_dir)
            _shf.copytree(font_dir, local_font_dir)
            print(f"[synth-gen] fonts {font_dir} -> {local_font_dir}", flush=True)
        if not local_corpus_dir.is_dir():
            local_corpus_dir.mkdir(parents=True, exist_ok=True)
        for src in [thai_corpus, english_corpus]:
            dst = local_corpus_dir / Path(src).name
            if not dst.is_file() or dst.stat().st_size != Path(src).stat().st_size:
                _shf.copy2(src, dst)
        thai_corpus = local_corpus_dir / Path(thai_corpus).name
        english_corpus = local_corpus_dir / Path(english_corpus).name
        font_dir = local_font_dir
    except Exception as e:
        print(f"[synth-gen] local copy failed {e}, using Drive paths", flush=True)
    # Config AFTER the local swap so workers read local /tmp paths, never Drive.
    cfg = out_dir / "config_multiline.yaml"
    corpus_paths = [thai_corpus, english_corpus]
    corpus_weights = [text_ratio, 1.0 - text_ratio]
    _write_config(cfg, corpus_paths, corpus_weights, font_dir, text_ratio, seed)
    _preflight_local(corpus_paths, corpus_weights, font_dir)
    print(f"[synth-gen] start n={n} workers={workers} template={Path(template).name} cfg={cfg.name} out={local_gen} -> {drive_gen} | in={thai_corpus.name},{english_corpus.name} fonts={font_dir}", flush=True)
    synth_log = out_dir / "synth_gen.log"
    # bg sync Drive every 30s while generating
    import threading as _th2
    _sync_stop = threading.Event()
    def _bg_sync():
        import shutil as _sh2, time as _tm
        while not _sync_stop.wait(30.0):
            try:
                if local_gen.is_dir():
                    if drive_gen.exists():
                        _sh2.rmtree(drive_gen)
                    _sh2.copytree(local_gen, drive_gen)
            except Exception as e:
                print(f"[synth-gen] bg sync failed {e}", flush=True)
    _bg_th = _th2.Thread(target=_bg_sync, daemon=True)
    _bg_th.start()
    cmd = [
        sys.executable,
        "-m",
        "synthtiger",
        "-o",
        str(local_gen),
        "-c",
        str(n),
        "-w",
        str(workers),
        "-s",
        str(seed),
    ]
    if debug:
        cmd.append("-v")  # surface swallowed per-sample tracebacks in synth_gen.log
    cmd += [template, "Multiline", str(cfg)]
    try:
        with open(synth_log, "w", encoding="utf-8") as log_fh:
            subprocess.run(
                cmd,
                check=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            )
    except subprocess.TimeoutExpired:
        print(f"[error] synth-gen timed out after {timeout_s}s log={synth_log} | out={out_dir}", flush=True)
        try:
            print(Path(synth_log).read_text()[-4000:], flush=True)
        except Exception:
            pass
        raise
    except subprocess.CalledProcessError as err:
        print(f"[error] synth-gen failed code={err.returncode} log={synth_log} | out={out_dir}", flush=True)
        try:
            print(Path(synth_log).read_text()[-2000:], flush=True)
        except Exception:
            pass
        raise
    finally:
        stop_evt.set()
        th.join(timeout=1.0)
        _sync_stop.set()
        _bg_th.join(timeout=1.0)
        # final sync local -> drive
        try:
            import shutil as _sh3
            if drive_gen.exists():
                _sh3.rmtree(drive_gen)
            _sh3.copytree(local_gen, drive_gen)
            print(f"[synth-gen] final sync {local_gen} -> {drive_gen}", flush=True)
        except Exception as e:
            print(f"[synth-gen] final sync failed {e}", flush=True)
        try:
            final_cnt = sum(1 for _ in gen_root.rglob("*.jpg"))
            final_cnt += sum(1 for _ in gen_root.rglob("*.png"))
        except Exception:
            final_cnt = 0
        if final_cnt > prog.n:
            prog.update(final_cnt - prog.n)
        prog.close()
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
