# GOaT Model — Colab Pro 1-Month

Each step is one file: `notebooks/<mainstep>/<step>.py` — run by shell (`uv run`) and by prod ipynb (`%run` with Drive args). Only runner differs: shell uses `uv`, ipynb uses `%run`; same file, same results.

## Steps

| Step file | Output |
|---|---|
| `notebooks/data/download_data` | `datasets/mt/`, `datasets/ocr/` |
| `notebooks/selection/select_mt` | `results/mt_selection.json` |
| `notebooks/selection/select_ocr` | `results/ocr_selection.json` |
| `notebooks/training/train_mt` | `results/mt_training.json` + `artifacts/mt_lora/` |
| `notebooks/training/train_ocr` | `results/ocr_training.json` + `artifacts/ocr/` |

`notebooks/selection.ipynb` (steps 6–8: download + selection) and `notebooks/training.ipynb` (steps 9–10: training) are the orchestrators — each `%run`s `notebooks/<mainstep>/<step>.py` with Drive args in order. No per-step `.ipynb`; one `.py` per step.

## Way 1: CLI (all from terminal, close-safe)

```bash
colab new -s goat --gpu T4
colab drivemount -s goat
colab install -s goat -r requirements.txt
```

Same package set is also in both notebooks' `%pip -r requirements.txt` cells as fallback, so the run works whether or not the install step was run. All Drive paths, repeats, and seed come from `src/goat_model/constants.py` (`DRIVE_ROOT`, `DRIVE_PATHS`, `SEED`, `MT_N_RUNS`, `OCR_N_RUNS`) — never hardcoded.

Open your console and paste (one block at a time):

```bash
colab console -s goat
```

```bash
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
```

Pushed code needs no manual upload — pull it on the VM (paste in console):

```bash
git -C /content/GOaT pull --ff-only
```

`git pull` covers every pushed file (both notebooks, `requirements.txt`, all step files).

Then launch (paste in console). Close the console whenever — `nohup ... &` detaches and the keep-alive daemon holds the VM:

```bash
mkdir -p /content/drive/MyDrive/GOaT/logs
nohup bash /content/GOaT/model/notebooks/selection.sh > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
```

Weights cache lives at `GOaT/hf_cache/` on Drive (`HF_HOME`/`HF_HUB_CACHE` exported by both `.sh` runners) — first run downloads (gated sets still need `HF_TOKEN`), reruns reuse with zero re-download. Slower per-file than SSD, faster than GBs over network.

Logs stay on local SSD (fast) with a copy synced to `GOaT/logs/` every 5 min — never point the live log straight at Drive (`tqdm` rewrites many times per second; each write would become a network roundtrip). After selection finishes, launch training (paste in console):

```bash
nohup bash /content/GOaT/model/notebooks/training.sh > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
```

Watch everything with one line (log + CPU/MEM/disk/GPU, paste in console, `Ctrl+C` stops the view only):

```bash
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader; ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr|train_mt|train_ocr" | head -1)'
```

Training run: same line with `/tmp/goat_training_log.txt` in place of `/tmp/goat_log.txt` (resource half already matches all four step names; both watches can run side by side).

Keep the host alive (laptop terminal — the keep-alive daemon dies with sleep/WiFi drops, and the VM follows). Bash:

```bash
while true; do colab sessions >/dev/null 2>&1; colab status -s goat 2>&1 | head -3; sleep 30; done
```

Fish:

```fish
while true
  colab sessions >/dev/null 2>&1; colab status -s goat 2>/dev/null | head -5
  sleep 30
end
```

Plus `systemd-inhibit --what=idle sleep infinity &` against idle suspend.

From anywhere (no VM needed), read the Drive copies (≤5 min stale):

```bash
tail -n 50 /content/drive/MyDrive/GOaT/logs/goat_log.txt
tail -n 50 /content/drive/MyDrive/GOaT/logs/goat_training_log.txt
```

Or without console: `colab log -s goat`. Re-running is safe — every step skips when its output JSON already exists, so no step burns GPU twice.

If any command ever fails with `module 'jupyter_kernel_client' has no attribute 'KernelClient'`, re-pin the dep (unpinned `jupyter-kernel-client` resolves to 1.x, which renamed the class `colab-cli 0.6.0` imports):
```bash
uv pip install --python ~/.local/share/uv/tools/google-colab-cli/bin/python "jupyter-kernel-client==0.15.0"
```

`notebooks/selection.sh` (twin of `selection.ipynb`) and `notebooks/training.sh` (twin of `training.ipynb`) call only step files with Drive args from constants:

```bash
uv run python notebooks/data/download_data.py --dataset scb-mt --out-dir "$DRIVE/datasets/mt"
uv run python notebooks/data/download_data.py --dataset flores200 --out-dir "$DRIVE/datasets/mt/test"
uv run python notebooks/data/download_data.py --dataset thaiocrbench --out-dir "$DRIVE/datasets/ocr/thaiocrbench"
uv run python notebooks/data/download_data.py --dataset thai-ocr-evaluation --out-dir "$DRIVE/datasets/ocr/thai-ocr-evaluation"
uv run python notebooks/selection/select_mt.py --mt-test-dir "$DRIVE/datasets/mt/test" --output "$DRIVE/results/mt_selection.json" --repeats "$REPEATS_MT" --seed "$SEED"
uv run python notebooks/selection/select_ocr.py --ocr-eval-dir "$DRIVE/datasets/ocr" --output "$DRIVE/results/ocr_selection.json" --repeats "$REPEATS_OCR" --seed "$SEED"
```

```bash
uv run python notebooks/training/train_mt.py --mt-dir "$DRIVE/datasets/mt" --selection "$DRIVE/results/mt_selection.json" --output "$DRIVE/results/mt_training.json" --out-root "$ART_MT" --seed "$SEED"
uv run python notebooks/training/train_ocr.py --selection "$DRIVE/results/ocr_selection.json" --data-root "$DRIVE/data" --output "$DRIVE/results/ocr_training.json" --out-root "$ART_OCR" --seed "$SEED"
```

Results at `GOaT/results/` on Drive. Stop: `colab stop -s goat`.

## Resume

**Same VM** (session alive, run died): pull first (picks up anything pushed since launch), then relaunch — steps skip finished outputs, datasets re-verify in place:

```bash
git -C /content/GOaT pull --ff-only
export HF_TOKEN=hf_paste_yours_here
nohup bash /content/GOaT/model/notebooks/selection.sh > /tmp/goat_log.txt 2>&1 &
```

**New VM** (session lost, `404/401`): re-provision, then everything resumes from Drive state:

```bash
colab new -s goat --gpu T4
colab drivemount -s goat
colab console -s goat
```

```bash
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
git -C /content/GOaT pull --ff-only
ls /content/drive/MyDrive/GOaT/results 2>&1
export HF_TOKEN=hf_paste_yours_here
mkdir -p /content/drive/MyDrive/GOaT/logs
nohup bash /content/GOaT/model/notebooks/selection.sh > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
```

## Way 2: VS Code (all from editor, click run)

1. Open `GOaT/` in VS Code
2. `Select Kernel` > `Colab` > `New Colab Server` > `T4`
3. Open `model/notebooks/selection.ipynb` > Run All, then `model/notebooks/training.ipynb` > Run All

Or open any `model/notebooks/<mainstep>/<step>.py` and Run File for that step alone.

Close VS Code while a cell is running — Pro+ keeps VM 24h.

Stop: `Colab: Stop Server` in VS Code.

## Outputs (same for both ways)

| File | Content |
|---|---|
| `results/mt_selection.json` | `selected` MT model |
| `results/ocr_selection.json` | `selected` OCR model |
| `results/mt_training.json` | winner LoRA config + FLORES BLEU |
| `results/ocr_training.json` | winner LR/batch + CER |

Parity verified by diffing the 4 JSONs (`selected`, `grid_results`) between both ways.
