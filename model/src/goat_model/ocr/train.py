"""Full-weight OCR fine-tuning for ThaiTrOCR.

Extracts the grid-search training logic out of the notebook so the Colab step
only imports and runs. SynthTIGER generation and data flattening live in
goat_model.synth_ocr; this module covers dataset prep, the Seq2SeqTrainer grid,
validation CER tracking and result writing.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
from datasets import Dataset
from PIL import Image as PILImage
from transformers import (
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    TrOCRProcessor,
    VisionEncoderDecoderModel,
    default_data_collator,
)

from goat_model.constants import (
    OCR_EARLY_STOP_METRIC,
    OCR_EARLY_STOP_PATIENCE,
    OCR_GRID_BATCH_SIZES,
    OCR_GRID_EPOCHS,
    OCR_GRID_LEARNING_RATES,
    OCR_IMG_SIZE,
    SEED,
    THAITROCR_MODEL_ID,
)
from goat_model.metrics import cer
from goat_model.utils import LogProgress, setup_seed, write_json

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _build_dataset(split_dir: Path, processor: TrOCRProcessor, img_size: int) -> Dataset:
    images, texts = [], []
    for img in sorted(split_dir.iterdir()):
        if img.suffix.lower() not in IMG_EXTS:
            continue
        gt = split_dir / f"{img.stem}.txt"
        if gt.is_file():
            images.append(str(img))
            texts.append(gt.read_text(encoding="utf-8").strip())
    ds = Dataset.from_dict({"image": images, "text": texts})

    def preprocess(ex):
        return {
            "pixel_values": processor(
                PILImage.open(ex["image"]).convert("RGB").resize((img_size, img_size)),
                return_tensors="pt",
            ).pixel_values[0],
            "labels": processor(ex["text"], return_tensors="pt").input_ids[0],
        }

    return ds.map(preprocess, remove_columns=["image", "text"])


def _compute_cer(eval_preds, processor: TrOCRProcessor) -> dict:
    preds, labels = eval_preds
    if isinstance(preds, tuple):
        preds = preds[0]
    preds = torch.tensor(preds)
    labels = torch.tensor(labels)
    labels[labels == -100] = processor.tokenizer.pad_token_id
    decoded_preds = processor.batch_decode(preds, skip_special_tokens=True)
    decoded_labels = processor.batch_decode(labels, skip_special_tokens=True)
    mean_cer = sum(cer(a, b) for a, b in zip(decoded_labels, decoded_preds))
    return {"cer": round(mean_cer / max(len(decoded_preds), 1), 4)}


def _infer_cer(
    model: VisionEncoderDecoderModel,
    test_ds: Dataset,
    processor: TrOCRProcessor,
    batch_size: int,
    refs: list[str],
) -> float:
    hyps: list[str] = []
    batches = range(0, len(test_ds), batch_size)
    prog = LogProgress(len(batches), "ocr-infer", unit="batch", interval_s=1.0)
    with torch.inference_mode():
        for i in batches:
            px = torch.stack(
                [torch.tensor(x) for x in test_ds[i : i + batch_size]["pixel_values"]]
            ).to(model.device)
            gen = model.generate(px, max_length=128)
            hyps.extend(processor.batch_decode(gen, skip_special_tokens=True))
            prog.update()
    prog.close()
    return sum(cer(a, b) for a, b in zip(refs, hyps)) / max(len(hyps), 1)


def run_ocr_finetune(
    data_root: Path,
    out_root: Path,
    result_path: Path,
    selected_model: str,
    seed: int = SEED,
) -> None:
    """Sweep full-weight ThaiTrOCR over the learning rate x batch grid.

    Loads the train/val/test dirs produced by ``split_ocr`` under ``data_root``,
    trains each grid config with early stopping on validation CER, then writes
    the winner and per-config results to ``result_path``.
    """
    if result_path.is_file():
        print(f"skipped - already trained: {result_path}")
        return

    if selected_model != "ThaiTrOCR":
        write_json(
            result_path,
            {
                "selected": selected_model,
                "skipped": "only ThaiTrOCR is fine-tuned; PP-OCRv5-mobile stays frozen",
            },
        )
        print(f"no fine-tune needed - selected {selected_model} stays frozen")
        return

    setup_seed(seed)
    processor = TrOCRProcessor.from_pretrained(THAITROCR_MODEL_ID)
    img_size = OCR_IMG_SIZE["ThaiTrOCR"]

    train_ds = _build_dataset(data_root / "train", processor, img_size)
    val_ds = _build_dataset(data_root / "val", processor, img_size)
    test_ds = _build_dataset(data_root / "test", processor, img_size)
    test_refs = [t for t in test_ds["text"]]

    partial_path = result_path.with_name(result_path.stem + ".partial.json")
    grid_results = {}
    best = None
    if partial_path.is_file():
        try:
            saved = json.loads(partial_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
        if saved.get("seed") == seed and saved.get("selected") == selected_model:
            grid_results = saved.get("grid_results", {})
            for v in grid_results.values():
                key = {k: v[k] for k in ("lr", "batch_size")}
                if best is None or v["cer"] < best[0]:
                    best = (v["cer"], key)
            if grid_results:
                print(f"[ocr-train] resuming {len(grid_results)} configs from {partial_path}", flush=True)
    total = len(OCR_GRID_LEARNING_RATES) * len(OCR_GRID_BATCH_SIZES)
    done = 0
    for lr in OCR_GRID_LEARNING_RATES:
        for batch in OCR_GRID_BATCH_SIZES:
            done += 1
            cfg_key = f"lr{lr}_bs{batch}"
            if cfg_key in grid_results:
                print(f"[ocr-train] skip done {cfg_key}", flush=True)
                continue
            print(f"[ocr-train] [{done}/{total}] lr={lr} bs={batch} — loading model", flush=True)
            setup_seed(seed)
            model = VisionEncoderDecoderModel.from_pretrained(THAITROCR_MODEL_ID)
            model.config.decoder_start_token_id = processor.tokenizer.cls_token_id
            model.config.pad_token_id = processor.tokenizer.pad_token_id
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = model.to(device)

            out_dir = out_root / f"lr{lr}_bs{batch}"
            args = Seq2SeqTrainingArguments(
                output_dir=str(out_dir),
                learning_rate=lr,
                per_device_train_batch_size=batch,
                per_device_eval_batch_size=batch,
                num_train_epochs=OCR_GRID_EPOCHS[1],
                optim="adamw_torch",
                eval_strategy="epoch",
                save_strategy="epoch",
                save_total_limit=1,
                load_best_model_at_end=True,
                metric_for_best_model=OCR_EARLY_STOP_METRIC,
                greater_is_better=False,
                early_stopping_patience=OCR_EARLY_STOP_PATIENCE,
                predict_with_generate=True,
                seed=seed,
                logging_steps=10,
                disable_tqdm=False,
            )
            trainer = Seq2SeqTrainer(
                model=model,
                args=args,
                train_dataset=train_ds,
                eval_dataset=val_ds,
                tokenizer=processor.feature_extractor,
                data_collator=default_data_collator,
                compute_metrics=lambda ep: _compute_cer(ep, processor),
            )
            trainer.train(resume_from_checkpoint=True)
            model.save_pretrained(out_dir)

            val_cer = _infer_cer(model, test_ds, processor, batch, test_refs)
            del model
            torch.cuda.empty_cache()

            key = {"lr": lr, "batch_size": batch}
            grid_results[cfg_key] = {**key, "cer": val_cer, "model": str(out_dir)}
            write_json(partial_path, {"seed": seed, "selected": selected_model, "grid_results": grid_results})
            print(f"config lr{lr} bs{batch}: CER {val_cer}", flush=True)
            if best is None or val_cer < best[0]:
                best = (val_cer, key)

    assert grid_results, "no grid config evaluated"
    assert best is not None, "no grid config evaluated"
    partial_path.unlink(missing_ok=True)
    write_json(
        result_path,
        {
            "selected": selected_model,
            "base_model": THAITROCR_MODEL_ID,
            "epochs": list(OCR_GRID_EPOCHS),
            "grid_results": grid_results,
            "winner": best[1],
            "winner_cer": best[0],
        },
    )
    print(f"wrote {result_path}", flush=True)
