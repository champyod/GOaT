"""Project-wide constants and decisions shared by all scripts.

Values here mirror GOaT-Documents/sections/GOAT-proposal (methodology.typ,
analysis.typ, scope.typ, hypothesis.typ). Update the docs AND this file
together.
"""

from __future__ import annotations

from pathlib import Path

MODEL_ROOT = Path(__file__).resolve().parents[2]

DATA = MODEL_ROOT / "data"
RESULTS = MODEL_ROOT / "results"

# ---------------------------------------------------------------------------
# Colab / Drive production paths (single source of truth).
# Every Drive path / artifact dir in notebooks/*.py, selection.ipynb Args cell,
# and notebooks/selection.sh MUST import these — never hardcode literals.
# ---------------------------------------------------------------------------
DRIVE_ROOT = Path("/content/drive/MyDrive/GOaT")

ART_MT = Path("/content/artifacts/mt_lora")
ART_OCR = Path("/content/artifacts/ocr")

DRIVE_PATHS = {
    "mt": DRIVE_ROOT / "datasets" / "mt",
    "mt_test": DRIVE_ROOT / "datasets" / "mt" / "test",
    "ocr_eval": DRIVE_ROOT / "datasets" / "ocr",
    "ocr_thaiocrbench": DRIVE_ROOT / "datasets" / "ocr" / "thaiocrbench",
    "ocr_thai_eval": DRIVE_ROOT / "datasets" / "ocr" / "thai-ocr-evaluation",
    "results": DRIVE_ROOT / "results",
    "data_root": DRIVE_ROOT / "data",
    "mt_selection": DRIVE_ROOT / "results" / "mt_selection.json",
    "ocr_selection": DRIVE_ROOT / "results" / "ocr_selection.json",
    "mt_training": DRIVE_ROOT / "results" / "mt_training.json",
    "ocr_training": DRIVE_ROOT / "results" / "ocr_training.json",
    "art_mt": ART_MT,
    "art_ocr": ART_OCR,
}

SYNTHETIC = DATA / "synthetic"
REAL = DATA / "real"
TRAIN = DATA / "train"
VAL = DATA / "val"
TEST = DATA / "test"
MT_TEST = DATA / "mt" / "test"
OCR_EVAL = DATA / "ocr"

SEED = 42

# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
OCR_MODELS = ("PP-OCRv5-mobile", "ThaiTrOCR")
OCR_DATASETS = ("thaiocrbench", "thai-ocr-evaluation")

# Per-model input resolution (bilinear resize before inference).
OCR_IMG_SIZE = {"PP-OCRv5-mobile": 512, "ThaiTrOCR": 384}

# Hardware budget for the selection experiments.
OCR_HW_CORES = 4
OCR_HW_RAM_GB = 8

# Selection decision rule: prefer ThaiTrOCR iff CER <= 0.10, else lowest CER.
OCR_CER_THRESHOLD = 0.10
OCR_N_RUNS = 5
OCR_ALPHA = 0.05

# Fine-tuning (grid search) ranges.
OCR_GRID_LEARNING_RATES = (1e-5, 2e-5, 3e-5, 4e-5, 5e-5)
OCR_GRID_BATCH_SIZES = (8, 16, 32)
OCR_GRID_EPOCHS = (10, 50)
OCR_EARLY_STOP_PATIENCE = 5

# Data augmentation used for the 10.5k-image fine-tune set.
AUG_ROTATION_DEG = 3.0
AUG_BRIGHTNESS_FRAC = 0.20
AUG_GAUSSIAN_SIGMA = 5.0

# OCR fine-tuning (step 10). Only ThaiTrOCR (the HF-trainable candidate) is
# fine-tuned, with full weights; the PP-OCRv5-mobile pipeline stays frozen.
THAITROCR_MODEL_ID = "openthaigpt/thai-trocr"
OCR_REAL_SCREEN_DIR = ""  # empty => synthetic-only; set to real screens dir when captured (500 images)
OCR_SYNTHETIC_N = 10_000
OCR_FONTS = (
    "TH Sarabun PSK",
    "Noto Sans Thai",
    "Kanit",
    "Prompt",
    "Sriracha",
)
OCR_WIKI_LANGS = ("th", "en")  # Thai 50% / English 50% from Wikipedia
OCR_EARLY_STOP_METRIC = "eval_cer"

# ---------------------------------------------------------------------------
# MT
# ---------------------------------------------------------------------------
MT_MODELS = ("NLLB-200-distilled-600M", "NLLB-200-distilled-1.3B")

# FLORES-200 or NLLB language codes.
LANG_CODES = {"th": "tha_Thai", "en": "eng_Latn"}

# Selection decision rule: prefer 600M iff BLEU > 35 AND latency <= 2 s, else 1.3B.
MT_BLEU_THRESHOLD = 35.0
MT_LATENCY_THRESHOLD_S = 2.0
MT_N_RUNS = 5
MT_ALPHA = 0.05

# Inference defaults (analysis.typ).
MT_BEAM_SIZE = 4
MT_MAX_LENGTH = 256
MT_LENGTH_PENALTY = 1.0
MT_BATCH_SIZE = 16

# LoRA fine-tuning ranges. Rank and alpha are folded to single values (the
# literature couples alpha to rank as a scale factor, alpha ~ 2r); only the
# learning rate is swept. Rank 12 and alpha 32 keep alpha in the standard
# 1-3x rank band while staying a sound capacity choice for a 600M model.
# The finding that modest rank (e.g. 4-16) matches full fine-tuning, so rank
# sensitivity is low, is from Hu et al., LoRA: Low-Rank Adaptation of Large
# Language Models, ICLR 2022, doi:10.48550/arXiv.2106.09685.
LORA_RANKS = (12,)
LORA_ALPHAS = (32,)
LORA_TARGET_MODULES = ("q_proj", "v_proj")
LORA_LEARNING_RATES = (2e-4, 3e-4, 4e-4, 5e-4)
LORA_EPOCHS = (3, 10)

# ---------------------------------------------------------------------------
# Splits (stratified, per Raschka 2018)
# ---------------------------------------------------------------------------
TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
TEST_RATIO = 0.15

DATA_PLAN = {
    "synthetic": 10_000,
    "real": 500,
    "train": 7_350,
    "val": 1_575,
    "test": 1_575,
}

# SCB-MT-EN-TH sampling plan.
SCBMT_TOTAL = 1_000_000
SCBMT_SAMPLE = 100_000
SCBMT_TRAIN = 80_000
SCBMT_VAL = 10_000
SCBMT_TEST = 10_000
