#!/usr/bin/env python3
"""CLI wrapper around goat_model.data - all logic lives in the library.

  --dataset scb-mt                 SCB-MT-EN-TH (HF pythainlp/scb_mt_enth_2020) -> data/mt/
  --dataset flores200              FLORES-200 th/en devtest -> data/mt/test/flores200.th|.en
  --dataset thaiocrbench           scb10x/ThaiOCRBench transcription tasks -> canonical layout
  --dataset thai-ocr-evaluation    openthaigpt/thai-ocr-evaluation -> canonical layout
  --ingest DIR                     manual fallback: copy images+same-stem .txt from DIR instead
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.data import (
    download_flores200,
    download_scbmt,
    download_thaiocr_evaluation,
    download_thaiocrbench,
    ingest_manual,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download/ingest GOaT datasets.")
    parser.add_argument(
        "--dataset",
        choices=("scb-mt", "flores200", "thaiocrbench", "thai-ocr-evaluation"),
        required=True,
    )
    parser.add_argument("--ingest", type=Path, help="manual source dir of images+gt")
    parser.add_argument("--out-dir", type=Path, default=None, help="override output directory")
    args = parser.parse_args()

    if args.dataset == "flores200":
        download_flores200(args.out_dir or c.MT_TEST)
    elif args.dataset == "scb-mt":
        download_scbmt(args.out_dir or c.DATA / "mt")
    elif args.dataset == "thaiocrbench":
        if args.ingest:
            ingest_manual("thaiocrbench", args.ingest)
        else:
            download_thaiocrbench(args.out_dir or c.OCR_EVAL / "thaiocrbench")
    elif args.dataset == "thai-ocr-evaluation":
        if args.ingest:
            ingest_manual("thai-ocr-evaluation", args.ingest)
        else:
            download_thaiocr_evaluation(args.out_dir or c.OCR_EVAL / "thai-ocr-evaluation")


if __name__ == "__main__":
    main()
