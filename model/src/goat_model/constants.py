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

# Data augmentation used for the 15k-image fine-tune set.
AUG_ROTATION_DEG = 3.0
AUG_BRIGHTNESS_FRAC = 0.20
AUG_GAUSSIAN_SIGMA = 5.0

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

# LoRA fine-tuning ranges.
LORA_RANKS = (8, 12, 16)
LORA_ALPHAS = (16, 32, 48)
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
