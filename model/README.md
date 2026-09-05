# GOaT Model

Offline OCR + machine-translation pipeline for the GOaT desktop app.
Follows `GOaT-Documents/sections/GOAT-proposal` (methodology.typ /
analysis.typ). Two candidate OCR models (PP-OCRv5-mobile, ThaiTrOCR) and two
MT models (NLLB-200 distilled 600M / 1.3B) are benchmarked, fine-tuned as
needed, then exported for the app.

## Layout

```
model/
  main.py                 CI smoke test entry (uv run main.py)
  pyproject.toml          base deps + ocr/mt/train extras
  src/goat_model/         shared pipeline library
    constants.py          every number that mirrors the proposal
    metrics.py            CER / word accuracy / BLEU / t-test / Cohen's d / bootstrap CI
    ocr/ engine.py        PaddleOCRv5 + ThaiTrOCR backends (lazy imports)
    ocr/ evaluate.py      per-image eval + run aggregation
    mt/  engine.py        NLLB backends (lazy imports)
    mt/  evaluate.py      corpus + per-domain BLEU, throughput, latency
  scripts/
    download_data.py      flores200 / scb-mt auto; Thai OCR datasets manual ingest
    generate_synthetic.py downloads 10k synthetic images from HF, then 70/15/15 split
    split_data.py         stratified 70/15/15 -> data/{train,val,test}
    eval_ocr.py           CER / word accuracy / latency  (proposal CLI)
    eval_mt.py            BLEU / throughput / latency    (proposal CLI)
    select_ocr.py         decision rule: ThaiTrOCR iff CER <= 0.10
    select_mt.py          decision rule: 600M iff BLEU > 35 AND <= 2s
    train_ocr.py          grid search + early stopping -> ONNX (TODO)
    train_mt.py           LoRA grid (frozen weights) -> CTranslate2 (TODO)
    export_models.py      OCR->ONNX, MT->CTranslate2 + smoke test
    monitor_ram.py        1 Hz RSS sampler (analysis.typ)
  data/                   ignored; see data/README contract below
  results/                ignored; JSON reports from all scripts
```

## Setup

```sh
uv sync                      # lightweight base deps (CI-safe smoke test)
uv run main.py               # CI smoke test
# heavy runtimes, opt-in:
uv sync --extra ocr          # paddleocr + paddlepaddle + onnxruntime + torch
uv sync --extra mt           # transformers + datasets + ctranslate2
uv sync --extra train        # peft + accelerate + datasets
```

## Data contract

- OCR eval datasets live under `data/ocr/<dataset>/images/<stem>.png` with
  ground truth `gt/<stem>.txt`. `thaiocrbench` and `thai-ocr-evaluation`
  currently have **no automated download** — ingest manually with
  `download_data.py --dataset <name> --ingest <dir>` (the paper source for
  each dataset still needs confirming).
- `data/mt/test/flores200.th|.en` (one sentence per line) for MT eval;
  optional `flores200.domains` for the per-domain BLEU split.

## Canonical commands (from analysis.typ)

```sh
python scripts/eval_ocr.py --model PP-OCRv5-mobile --dataset thaiocrbench \
    --output ./results/ocr.json --device cpu --seed 42
python scripts/eval_mt.py --model NLLB-200-distilled-600M --src en --tgt th \
    --dataset flores200 --output ./results/mt.json --device cpu \
    --beam 4 --max_len 256 --seed 42
python scripts/monitor_ram.py --pid <goat_pid> --duration 60 --output results/ram.json
```

## Open decisions

- **Translation direction**: resolved to **EN->TH** (per `scope.typ`); the
  eval defaults and commands above use `--src en --tgt th`. `analysis.typ`
  still shows th->en examples - update the doc to match.
- Python was pinned to **3.12** (`.python-version`, and CI overrides to 3.12)
  because 3.14 wheels are missing for the ML deps; the proposal text still says
  3.13.
- `thaiocrbench` / `thai-ocr-evaluation` now auto-download from
  `scb10x/ThaiOCRBench` (transcription tasks only) and
  `openthaigpt/thai-ocr-evaluation`; `--ingest` remains as a manual fallback.