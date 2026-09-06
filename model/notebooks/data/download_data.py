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
import json
import traceback
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model import constants as c
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import load_dotenv
from goat_model.utils import log_call
from goat_model.data import (
    download_flores200,
    download_scbmt,
    download_thaiocr_evaluation,
    download_thaiocrbench,
    ingest_manual,
)



@log_call
def _has_images(out_dir: Path) -> bool:
    images = out_dir / "images"
    gt = out_dir / "gt"
    return images.is_dir() and gt.is_dir() and any(images.iterdir())


@log_call
def main() -> None:
    load_dotenv()
    _info("download-data", "start", script=Path(__file__).name)
    parser = argparse.ArgumentParser(description="Download/ingest GOaT datasets.")
    parser.add_argument(
        "--dataset",
        choices=("scb-mt", "flores200", "thaiocrbench", "thai-ocr-evaluation"),
        required=True,
    )
    parser.add_argument("--ingest", type=Path, help="manual source dir of images+gt")
    parser.add_argument("--out-dir", type=Path, default=None, help="override output directory")
    parser.add_argument("--force", action="store_true", help="re-download even when outputs exist")
    parser.add_argument("--debug", action="store_true", help="verbose per-action logs")
    args = parser.parse_args()
    _info("download-data", "args", **vars(args))
    _err_out = getattr(args, "out_dir", None)
    try:

        if args.dataset == "flores200":
            out = args.out_dir or c.MT_TEST
            if not args.force and (out / "flores200.th").is_file() and (out / "flores200.en").is_file():
                _info("download-data", "skipped - already downloaded", out=str(out))
            else:
                download_flores200(out)
        elif args.dataset == "scb-mt":
            out = args.out_dir or c.DATA / "mt"
            parts = [f"{n}.{e}" for n in ("train", "val", "test") for e in ("th", "en")]
            if not args.force and all((out / f).is_file() for f in parts):
                _info("download-data", "skipped - already downloaded", out=str(out))
            else:
                download_scbmt(out)
        elif args.dataset == "thaiocrbench":
            if args.ingest:
                ingest_manual("thaiocrbench", args.ingest)
            else:
                out = args.out_dir or c.OCR_EVAL / "thaiocrbench"
                if not args.force and _has_images(out):
                    _info("download-data", "skipped - already downloaded", out=str(out))
                else:
                    download_thaiocrbench(out)
        elif args.dataset == "thai-ocr-evaluation":
            if args.ingest:
                ingest_manual("thai-ocr-evaluation", args.ingest)
            else:
                out = args.out_dir or c.OCR_EVAL / "thai-ocr-evaluation"
                if not args.force and _has_images(out):
                    _info("download-data", "skipped - already downloaded", out=str(out))
                else:
                    download_thaiocr_evaluation(out)


        _info("download-data", "done", script=Path(__file__).name)


    except Exception as err:
        tb = traceback.format_exc()
        inp = getattr(args, "out_dir", "?")
        _err("download-data", "failed", inp=str(inp), out=str(_err_out), error=str(err))
        print(tb, flush=True)
        if _err_out is not None:
            try:
                _err_path = str(_err_out) + ".error.json"
                from pathlib import Path as _P
                _P(_err_path).write_text(json.dumps({"error": str(err), "kind": "download_data", "input": str(inp), "output": str(_err_out)}, indent=2))
                _info("download-data", "wrote error file", path=_err_path)
            except Exception:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
