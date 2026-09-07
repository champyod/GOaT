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
    EarlyStoppingCallback,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    TrOCRProcessor,
    VisionEncoderDecoderModel,
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
from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, LogProgress, resolve_device, setup_seed, trainer_heartbeat, write_json

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


@log_call
def _make_collator(processor: TrOCRProcessor):
    """Batch collator that never touches tokenizer.pad.

    DataCollatorForSeq2Seq routes leftover features through
    ``tokenizer.pad``, which demands ``input_ids`` and dies on vision-only
    rows. Stack pixels directly, pad labels with -100 (ignored in loss).
    """

    def collate(features):
        pixel_values = torch.stack(
            [
                f["pixel_values"]
                if torch.is_tensor(f["pixel_values"])
                else torch.tensor(f["pixel_values"])
                for f in features
            ]
        )
        labels = [torch.tensor(f["labels"]) for f in features]
        labels = torch.nn.utils.rnn.pad_sequence(
            labels, batch_first=True, padding_value=processor.tokenizer.pad_token_id
        )
        labels[labels == processor.tokenizer.pad_token_id] = -100
        return {"pixel_values": pixel_values, "labels": labels}

    return collate


@log_call
def _build_dataset(split_dir: Path, processor: TrOCRProcessor, img_size: int) -> tuple[Dataset, list[str]]:
    images, texts = [], []
    for img in sorted(split_dir.iterdir()):
        if img.suffix.lower() not in IMG_EXTS:
            continue
        gt = split_dir / f"{img.stem}.txt"
        if gt.is_file():
            images.append(str(img))
            texts.append(gt.read_text(encoding="utf-8").strip())
    ds = Dataset.from_dict({"image": images, "text": texts})

    def preprocess(batch):
        # NOTE: no "text" here - with_transform replaces row content with this
        # output, and raw strings would ride into DataCollatorForSeq2Seq's
        # tokenizer.pad. Test refs come from the stored "text" column instead.
        return {
            "pixel_values": [
                processor(
                    PILImage.open(p).convert("RGB").resize((img_size, img_size)),
                    return_tensors="pt",
                ).pixel_values[0]
                for p in batch["image"]
            ],
            "labels": processor.tokenizer(batch["text"]).input_ids,
        }

    # refs travel alongside: with_transform drops the stored "text" column,
    # so re-reading test_ds["text"] later would KeyError.
    return ds.with_transform(preprocess), texts


@log_call
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


@log_call
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


@log_call
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
        _info("ocr-train", "skipped - already trained", result=str(result_path))
        return

    if selected_model != "ThaiTrOCR":
        write_json(
            result_path,
            {
                "selected": selected_model,
                "skipped": "only ThaiTrOCR is fine-tuned; PP-OCRv5-mobile stays frozen",
            },
        )
        _info("ocr-train", "no fine-tune needed - stays frozen", selected=selected_model)
        return

    setup_seed(seed)
    processor = TrOCRProcessor.from_pretrained(THAITROCR_MODEL_ID)
    img_size = OCR_IMG_SIZE["ThaiTrOCR"]

    train_ds, _ = _build_dataset(data_root / "train", processor, img_size)
    val_ds, _ = _build_dataset(data_root / "val", processor, img_size)
    test_ds, test_refs = _build_dataset(data_root / "test", processor, img_size)

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
                _info("ocr-train", "resuming configs", done=len(grid_results), partial=str(partial_path))
    total = len(OCR_GRID_LEARNING_RATES) * len(OCR_GRID_BATCH_SIZES)
    done = 0
    for lr in OCR_GRID_LEARNING_RATES:
        for batch in OCR_GRID_BATCH_SIZES:
            done += 1
            cfg_key = f"lr{lr}_bs{batch}"
            if cfg_key in grid_results:
                _info("ocr-train", "skip done", config=cfg_key)
                continue
            _info("ocr-train", "loading model", done=done, total=total, lr=lr, batch=batch)
            setup_seed(seed)
            model = VisionEncoderDecoderModel.from_pretrained(THAITROCR_MODEL_ID)
            model.config.decoder_start_token_id = processor.tokenizer.cls_token_id
            model.config.pad_token_id = processor.tokenizer.pad_token_id
            device = resolve_device("cuda")
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
                predict_with_generate=True,
                seed=seed,
                logging_steps=10,
                disable_tqdm=False,
                remove_unused_columns=False,
            )
            trainer = Seq2SeqTrainer(
                model=model,
                args=args,
                train_dataset=train_ds,
                eval_dataset=val_ds,
                processing_class=processor.tokenizer,
                data_collator=_make_collator(processor),
                compute_metrics=lambda ep: _compute_cer(ep, processor),
                callbacks=[
                    trainer_heartbeat("ocr-train"),
                    EarlyStoppingCallback(early_stopping_patience=OCR_EARLY_STOP_PATIENCE),
                ],
            )
            from transformers.trainer_utils import get_last_checkpoint

            last_ckpt = get_last_checkpoint(out_dir)
            trainer.train(resume_from_checkpoint=last_ckpt if last_ckpt else False)
            model.save_pretrained(out_dir)

            val_cer = _infer_cer(model, test_ds, processor, batch, test_refs)
            del model
            torch.cuda.empty_cache()

            key = {"lr": lr, "batch_size": batch}
            grid_results[cfg_key] = {**key, "cer": val_cer, "model": str(out_dir)}
            write_json(partial_path, {"seed": seed, "selected": selected_model, "grid_results": grid_results})
            _info("ocr-train", "CER", lr=lr, batch=batch, cer=val_cer)
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
    _info("ocr-train", "wrote result", result=str(result_path))
