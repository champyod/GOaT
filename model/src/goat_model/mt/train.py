"""LoRA fine-tuning for NLLB-200-distilled-600M (selection winner).

Shared handler used by scripts/train_mt.py and notebooks/steps/train_mt.ipynb.
Grid over LoRA rank/alpha/LR, BLEU on FLORES-200 every epoch, best saved.
Only NLLB-600M is trained; 1.3B stays zero-shot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import torch
from tqdm import tqdm
from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model, PeftModel
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)

from goat_model.constants import (
    ART_MT,
    DRIVE_PATHS,
    LANG_CODES,
    LORA_ALPHAS,
    LORA_EPOCHS,
    LORA_LEARNING_RATES,
    LORA_RANKS,
    LORA_TARGET_MODULES,
    MT_BATCH_SIZE,
    MT_MAX_LENGTH,
    SEED,
)
from goat_model.metrics import corpus_bleu
from goat_model.mt.engine import NLLB_HF_IDS
from goat_model.mt.evaluate import load_pairs
from goat_model.utils import setup_seed, write_json


def run_mt_finetune(
    mt_dir: Path,
    selection_path: Path,
    result_path: Path,
    out_root: Path,
    seed: int = SEED,
) -> None:
    """Run LoRA grid for NLLB-600M; skip if result already exists."""
    if result_path.is_file():
        print(f"skipped - already trained: {result_path}")
        return

    MODEL_600M = "NLLB-200-distilled-600M"
    if not selection_path.is_file():
        raise SystemExit(f"missing selection: {selection_path} — run select_mt first")

    selected = json.loads(selection_path.read_text()).get("selected")
    if selected != MODEL_600M:
        write_json(result_path, {"selected": selected, "skipped": f"only {MODEL_600M} is fine-tuned"})
        print(f"no fine-tune needed - selected {selected} stays zero-shot")
        return

    setup_seed(seed)
    model_id = NLLB_HF_IDS[MODEL_600M]
    tokenizer = AutoTokenizer.from_pretrained(model_id, src_lang=LANG_CODES["en"], tgt_lang=LANG_CODES["th"])

    def load_split(name: str) -> Dataset:
        src, ref, _ = load_pairs(mt_dir / f"{name}.en", mt_dir / f"{name}.th")
        model_inputs = tokenizer(src, max_length=MT_MAX_LENGTH, truncation=True, padding=False)
        labels = tokenizer(text_target=ref, max_length=MT_MAX_LENGTH, truncation=True, padding=False)
        labels_ids = [[tok if tok != tokenizer.pad_token_id else -100 for tok in ids] for ids in labels["input_ids"]]
        return Dataset.from_dict({"input_ids": model_inputs["input_ids"], "attention_mask": model_inputs["attention_mask"], "labels": labels_ids})

    train_ds = load_split("train")
    val_ds = load_split("val")
    flores_src, flores_ref, _ = load_pairs(mt_dir / "test" / "flores200.en", mt_dir / "test" / "flores200.th")

    def compute_bleu(eval_preds) -> dict:
        preds, labels = eval_preds
        if isinstance(preds, tuple):
            preds = preds[0]
        preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
        labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
        decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)
        decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)
        return {"bleu": round(corpus_bleu(decoded_labels, decoded_preds), 4)}

    device = "cuda" if torch.cuda.is_available() else "cpu"
    grid_results: dict = {}
    best = None

    for r in LORA_RANKS:
        for alpha in LORA_ALPHAS:
            for lr in LORA_LEARNING_RATES:
                print(f"[mt-train] config {r}/{alpha}/{lr} — loading base", flush=True)
                setup_seed(seed)
                base = AutoModelForSeq2SeqLM.from_pretrained(model_id).to(device)
                peft_config = LoraConfig(task_type=TaskType.SEQ_2_SEQ_LM, r=r, lora_alpha=alpha, target_modules=list(LORA_TARGET_MODULES), lora_dropout=0.1, bias="none")
                model = get_peft_model(base, peft_config)
                collator = DataCollatorForSeq2Seq(tokenizer, model=model)
                out_dir = out_root / f"r{r}_alpha{alpha}_lr{lr}"
                args = Seq2SeqTrainingArguments(output_dir=str(out_dir), learning_rate=lr, per_device_train_batch_size=MT_BATCH_SIZE, num_train_epochs=LORA_EPOCHS[1], optim="adamw_torch", eval_strategy="epoch", save_strategy="epoch", save_total_limit=1, load_best_model_at_end=True, metric_for_best_model="eval_bleu", greater_is_better=True, predict_with_generate=True, seed=seed, logging_steps=10, disable_tqdm=False)
                trainer = Seq2SeqTrainer(model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds, tokenizer=tokenizer, data_collator=collator, compute_metrics=compute_bleu)
                trainer.train(resume_from_checkpoint=True)
                model.save_pretrained(out_dir)

                ft_model = PeftModel.from_pretrained(AutoModelForSeq2SeqLM.from_pretrained(model_id).to(device), out_dir)
                ft_model.eval()
                hyps: list[str] = []
                print(f"[mt-train] infer FLORES {len(flores_src)} sents, bs={MT_BATCH_SIZE}", flush=True)
                with torch.inference_mode():
                    for i in tqdm(range(0, len(flores_src), MT_BATCH_SIZE), desc=f"infer r{r} a{alpha} lr{lr}", unit="batch"):
                        batch = tokenizer(flores_src[i : i + MT_BATCH_SIZE], return_tensors="pt", padding=True, truncation=True).to(device)
                        gen = ft_model.generate(**batch, forced_bos_token_id=tokenizer.convert_tokens_to_ids(LANG_CODES["th"]), num_beams=4, max_length=MT_MAX_LENGTH)
                        hyps.extend(tokenizer.batch_decode(gen, skip_special_tokens=True))
                flores_bleu = corpus_bleu(flores_ref, hyps)
                del ft_model, base, model
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                key = {"rank": r, "alpha": alpha, "lr": lr}
                grid_results[f"r{r}_alpha{alpha}_lr{lr}"] = {**key, "flores_bleu": flores_bleu, "adapter": str(out_dir)}
                print(f"config r{r} alpha{alpha} lr{lr}: FLORES BLEU {flores_bleu}", flush=True)
                if best is None or flores_bleu > best[0]:
                    best = (flores_bleu, key)

    assert best is not None
    # local-first copy to Drive (paths from constants, never literals)
    try:
        shutil.copytree(Path(ART_MT).parent, DRIVE_PATHS["results"].parent / "artifacts", dirs_exist_ok=True)
    except OSError:
        subprocess.run(["fusermount", "-u", "/content/drive"], check=False)
        shutil.copytree(Path(ART_MT).parent, DRIVE_PATHS["results"].parent / "artifacts", dirs_exist_ok=True)

    write_json(result_path, {"selected": selected, "base_model": model_id, "target_modules": list(LORA_TARGET_MODULES), "epochs": list(LORA_EPOCHS), "grid_results": grid_results, "winner": best[1], "winner_flores_bleu": best[0]})
    print(f"wrote {result_path}", flush=True)
