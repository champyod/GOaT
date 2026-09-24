# GOaT Colab — Selection & Training

Flow: `data/` → SELECT (pick winner) → TRAIN (fine-tune winner) → EXPORT (app files).
Select grades both original models and writes `decision.selected`; train reads that file and trains only the winner.

## 0. Prereqs (once per VM)

```bash
colab new -s goat --gpu T4
colab drivemount -s goat
colab console -s goat
```

```bash
[ -f /content/GOaT/model/.env ] || cp /content/GOaT/model/.env.example /content/GOaT/model/.env
# fill HF_TOKEN (+ webhook URL) in /content/GOaT/model/.env once — picked up
# automatically, no re-export on every new console
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
git -C /content/GOaT pull --ff-only
mkdir -p /content/drive/MyDrive/GOaT/logs
```

## Selection (steps 6-8)

```bash
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1); echo ---; ps -eo pcpu,pmem,etime,args --sort=-%mem | head -8'
```

What `selection.sh` runs, in order: `uv sync --extra ocr --extra mt --extra train` → 4× `download_data.py` (scb-mt, flores200, thaiocrbench, thai-ocr-evaluation → Drive `datasets/`) → `select_mt.py` → `select_ocr.py`. `--test` shrinks MT to 3 repeats × batch 338 (smoke run); OCR untouched.

### Selection debug

Same commands with `--debug` on the script call — the flag fans out to every python step (per-action enter/exit, full args, tracebacks). `GOAT_DEBUG=1` works instead of the flag.
```bash
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1); echo ---; ps -eo pcpu,pmem,etime,args --sort=-%mem | head -8'
```
Isolated single-script reruns (from `/content/GOaT/model`, venv python — never bare `python3`):
```bash
uv run python notebooks/selection/select_mt.py --mt-test-dir $MT_TEST_DIR --output $RESULTS/mt_selection.json --repeats $REPEATS_MT --seed $SEED --debug
uv run python notebooks/selection/select_ocr.py --ocr-eval-dir $OCR_EVAL_DIR --output $RESULTS/ocr_selection.json --repeats $REPEATS_OCR --seed $SEED --debug
```

### Selection outputs (all next to `--output`, Drive `results/`)

- `mt_selection.json` / `ocr_selection.json` — the verdict: per-model mean±std+CI, comparisons, `decision.selected`. Train reads these.
- `*.samples_<runid>.jsonl` — every image/sentence: reference, hypothesis, full calculation trace, pass/fail verdict. Timestamped, never overwritten.
- `*.<runid>.result.json` — frozen copy of the verdict, same timestamp.
- `*.partial.json` — resume checkpoint (deleted on success); reruns pick up mid-list.
- `*.PP_OCRv5_mobile.json` / `*.ThaiTrOCR.json` (+ MT equivalents) — per-model summaries.
- `*.error.json` — only on crash: error + traceback pointer.

Check the verdict without opening JSON:
```bash
grep -h SELECTED /tmp/goat_log.txt | tail -2
uv run python -c "import json;print(json.load(open('/content/drive/MyDrive/GOaT/results/ocr_selection.json'))['decision'])"
```
Rerun rules: finished `--output` file = skip ("already selected"); `--force` reruns everything; interrupted runs resume from `.partial.json` automatically.

## Training (steps 9-10)

```bash
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
git -C /content/GOaT pull --ff-only
mkdir -p /content/drive/MyDrive/GOaT/logs

nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &

nohup python3 /content/GOaT/tools/vm_reaper.py --interval 300 > /tmp/vm_reaper.log 2>&1 &
tail -n 2 /tmp/vm_reaper.log

watch -n 3 'tail -n 15 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -5); echo ---; ps -eo pcpu,pmem,etime,args --sort=-%mem | head -8; echo ---; cat /content/drive/MyDrive/GOaT/logs/ram_train_ocr.json 2>/dev/null'
```

What `training.sh` runs, in order: `train_mt.py` (LoRA grid, best BLEU wins → `mt_training.json`) → `generate_synthetic.py` (10k SynthTIGER images, local-only) → `train_ocr.py` (fine-tunes the `ocr_selection.json` winner only → `ocr_training.json`). Checkpoints stream live to Drive (`/content/artifacts/{mt_lora,ocr}`) — a dead VM loses nothing.

RAM curve (paste after training starts; JSON lives on Drive, shown inside watch):
```bash
TRAIN_PID=$(pgrep -f "notebooks/training/train_ocr.py" | head -1)
nohup uv run --project /content/GOaT/model python scripts/monitor_ram.py --pid $TRAIN_PID --duration 86400 --interval 5 --output /content/drive/MyDrive/GOaT/logs/ram_train_ocr.json > /tmp/ram_watch.log 2>&1 &
```

### Training debug
Same commands with `--debug` on the script call — reaches train_mt, train_ocr and generate_synthetic.
```bash
nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &

until TRAIN_PID=$(pgrep -f "notebooks/training/train_ocr.py" | head -1) && [ -n "$TRAIN_PID" ]; do sleep 30; done
nohup uv run --project /content/GOaT/model python scripts/monitor_ram.py --pid "$TRAIN_PID" --duration 86400 --interval 5 --output /content/drive/MyDrive/GOaT/logs/ram_train_ocr.json > /tmp/ram_watch.log 2>&1 &

watch -n 3 'tail -n 15 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -5); echo ---; ps -eo pcpu,pmem,etime,args --sort=-%mem | head -8; echo ---; cat /content/drive/MyDrive/GOaT/logs/ram_train_ocr.json 2>/dev/null'
```
Isolated single-script rerun:
```bash
uv run python scripts/generate_synthetic.py --out /tmp/goat_synth_data/synthetic --debug
```
Notes:
- generate_synthetic downloads the 10k SynthTIGER images from the HF dataset (KunanonKhai/Synthetic-GOaT-OCR), flattens them, then 70/15/15 splits into /tmp/goat_synth_data/{train,val,test} for train_ocr.
- Synth never touches Drive (local-only, so log/result syncing never contends).
- train_mt skips when mt_training.json already exists (already-trained guard).

## Gotchas (learned from real runs)

- **Never bare `python3`.** Colab's system python lacks project deps — always `uv run python ...` or `.venv/bin/python3` from `model/`.
- **Sync both extras.** `uv sync --extra ocr --extra mt` — `ocr/__init__` imports training code that needs `datasets` (mt extra); ocr-only sync crashes with `ModuleNotFoundError: No module named 'datasets'`.
- **matplotlib backend.** If paddleocr dies with `Key backend: 'module://matplotlib_inline...'`, the kernel leaked its backend var: `export MPLBACKEND=agg` and rerun.
- **Gated sets need `HF_TOKEN`** in `model/.env` (public OCR/MT eval sets don't).
- **First run downloads GBs** (torch + CUDA wheels + paddle + weights) — the "loading weights" log line; reruns reuse `HF_HOME` on Drive, no re-download.
- **Logs are stdout/stderr only** — no log files unless you redirect (`> /tmp/goat_log.txt 2>&1`). `--debug` / `GOAT_DEBUG=1` for per-action lines. Secrets auto-redacted.

## Caches (safe to delete, auto-rebuilt)

- `model/.venv/` — packages (`uv sync` rebuilds).
- `~/.cache/uv` — download cache (redirect: `UV_CACHE_DIR=...`).
- `HF_HOME` on Drive — model weights (re-download on first use).
- `~/.paddlex/` — PP-OCRv5 weights (~20 MB total: 4.7 MB det + ~8–16 MB rec).

## Sync-watch (Pi operator)
One process pulls the VM log and watches it — no separate sync step:
```bash
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...  # or model/.env on this host
python GOaT/tools/sync-watch --vm-log /tmp/goat_training_log.txt --session goat \
    --out ~/synced/goat.log --silence 900 --downtime 3600 --poll 15
```
Tune with `--error-pattern` / `--done-pattern` (repeatable). Never exits: warns at `--silence`, errors at `--downtime`, quiet while sync stalled or after error/done. Notifications are fail-open: a dead webhook never stops the watch.

## Common
- Resume: `git pull` pulls new code; partials resume same VM (/tmp); new VM re-pulls deterministic HF data.
- Logs: `/tmp/*` fast, Drive `logs/` every 5m.
- **Stop when done (REQUIRED):** `colab stop -s goat` — check `colab sessions` shows nothing after.
- Keep host: `while true; do echo "--- $(date) ---"; colab exec -s goat <<< "print('ping')" >/dev/null 2>&1; colab sessions; colab status -s goat 2>&1 | head -5; sleep 30; done`
