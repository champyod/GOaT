#!/usr/bin/env python3
"""Paired before/after CER scoring for the ThaiTrOCR fine-tune.

Scores the zero-shot base weights and a fine-tuned checkpoint over one shared
image set with an identical decoder configuration, then runs the project's own
paired t-test across the per-image CERs. Inference only — no training step runs
here, and both model directories are read from local paths.

Both sides go through the same preprocessing as the fine-tuner
(``ocr/train.py``): ``convert("RGB").resize((384, 384))`` then
``generate(max_length=128)``, so the only thing that differs between the two
halves of the comparison is the weight file.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model.constants import OCR_IMG_SIZE, SEED, THAITROCR_MODEL_ID
from goat_model.log import info as _info
from goat_model.metrics import cer, cohens_d, paired_t_test
from goat_model.ocr.evaluate import discover_assets
from goat_model.utils import LogProgress, log_call, read_gt, resolve_device, setup_seed, write_json

# Matches goat_model/ocr/train.py::_infer_cer, the run that produced
# winner_cer in results/ocr_training.json; changing it would make the two
# sides of the comparison decode differently.
MAX_LENGTH = 128


@log_call
def _load_processor(processor_dir: Path):
    """Image processor + tokenizer, always from the base directory.

    Checkpoints written by Seq2SeqTrainer carry no preprocessor_config.json, so
    this is the only source for it — and it is the same source the fine-tuner
    used. Sharing one processor across both sides keeps the comparison paired.
    """
    from transformers import TrOCRProcessor

    return TrOCRProcessor.from_pretrained(str(processor_dir))


@log_call
def _load_weights(weights_dir: Path, device: str):
    """Load one TrOCR weight set from a local directory. Fails loudly on a
    missing weight file rather than silently scoring base weights."""
    from transformers import VisionEncoderDecoderModel

    weights = weights_dir / "model.safetensors"
    if not weights.is_file():
        raise FileNotFoundError(f"no model.safetensors in {weights_dir}")
    setup_seed(SEED)
    model = VisionEncoderDecoderModel.from_pretrained(str(weights_dir))
    model.eval()
    model.to(device)
    return model


@log_call
def _score(
    weights_dir: Path,
    processor,
    assets: list,
    device: str,
    img_size: int,
    seed: int,
) -> list[dict]:
    """Per-image hypothesis + CER for one weight directory, in asset order."""
    import torch
    from PIL import Image as PILImage

    model = _load_weights(weights_dir, device)
    prog = LogProgress(len(assets), f"ocr-score:{weights_dir.name}", unit="img", interval_s=15.0)
    records: list[dict] = []
    with torch.inference_mode():
        for asset in assets:
            start = time.perf_counter()
            pil = PILImage.open(asset.image).convert("RGB").resize((img_size, img_size))
            pixels = processor(images=pil, return_tensors="pt").pixel_values.to(device)
            generated = model.generate(pixels, max_length=MAX_LENGTH)
            hyp = processor.batch_decode(generated, skip_special_tokens=True)[0]
            ref = read_gt(asset.gt)
            records.append(
                {
                    "image_id": asset.image.stem,
                    "reference": ref,
                    "hypothesis": hyp,
                    "cer": cer(ref, hyp),
                    "latency_ms": (time.perf_counter() - start) * 1000.0,
                }
            )
            prog.update()
    prog.close()
    return records


@log_call
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="zero-shot weights (local dir)")
    parser.add_argument("--finetuned", type=Path, required=True, help="checkpoint dir (local)")
    parser.add_argument("--dataset", type=Path, required=True, help="dir with images/ and gt/")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    assets = discover_assets(args.dataset)
    if not assets:
        raise ValueError(f"no image/ground-truth pairs under {args.dataset}")
    device = resolve_device(args.device)
    img_size = OCR_IMG_SIZE["ThaiTrOCR"]
    setup_seed(args.seed)

    _info("ocr-paired", "dataset", path=str(args.dataset), n_images=len(assets), device=device)

    sides = {"base": args.base, "finetuned": args.finetuned}
    processor = _load_processor(args.base)
    scored = {
        name: _score(path, processor, assets, device, img_size, args.seed)
        for name, path in sides.items()
    }

    base_cer = [rec["cer"] for rec in scored["base"]]
    ft_cer = [rec["cer"] for rec in scored["finetuned"]]

    def mean_sd(values: list[float]) -> dict:
        return {
            "n": len(values),
            "mean_cer": sum(values) / len(values),
            "sd": (sum((v - sum(values) / len(values)) ** 2 for v in values) / (len(values) - 1))
            ** 0.5,
        }

    test = paired_t_test(base_cer, ft_cer)
    write_json(
        args.output,
        {
            "provenance": {
                "base_model_id": THAITROCR_MODEL_ID,
                "base_dir": str(args.base),
                "finetuned_dir": str(args.finetuned),
                "dataset_dir": str(args.dataset),
                "n_images": len(assets),
                "seed": args.seed,
                "device": device,
                "img_size": img_size,
                "max_length": MAX_LENGTH,
                "decoder_sampling": "greedy (num_beams=1, do_sample=False)",
                "cer": "goat_model.metrics.cer, max(len(ref), len(hyp)) denominator",
            },
            "summary": {"base": mean_sd(base_cer), "finetuned": mean_sd(ft_cer)},
            "paired_test": {
                "sign_convention": "mean_diff = mean(base) - mean(finetuned); t = ttest_rel(base, finetuned)",
                "mean_diff": (sum(base_cer) - sum(ft_cer)) / len(base_cer),
                **test,
                "cohens_d": cohens_d(base_cer, ft_cer),
            },
            "images": [
                {
                    "image_id": base_rec["image_id"],
                    "base_cer": base_rec["cer"],
                    "finetuned_cer": ft_rec["cer"],
                    "reference": base_rec["reference"],
                    "base_hypothesis": base_rec["hypothesis"],
                    "finetuned_hypothesis": ft_rec["hypothesis"],
                }
                for base_rec, ft_rec in zip(scored["base"], scored["finetuned"], strict=True)
            ],
        },
    )
    _info(
        "ocr-paired",
        "mean CER",
        base=round(sum(base_cer) / len(base_cer), 4),
        finetuned=round(sum(ft_cer) / len(ft_cer), 4),
    )
    _info("ocr-paired", "paired t-test", p=test["p_value"], t=test["t_statistic"])
    _info("ocr-paired", "wrote", out=str(args.output))


if __name__ == "__main__":
    main()
