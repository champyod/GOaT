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

`notebooks/selection.ipynb` is the orchestrator — it `%run`s `notebooks/<mainstep>/<step>.py` with Drive args in order. No per-step `.ipynb`; one `.py` per step.

## Way 1: CLI (all from terminal, close-safe)

```bash
colab new -s goat --gpu T4
colab drivemount -s goat
colab install -s goat -r requirements.txt
```

Same package set is also in `selection.ipynb` `%pip` cells as fallback, so the run works whether or not the install step was run. All Drive paths, repeats, and seed come from `src/goat_model/constants.py` (`DRIVE_ROOT`, `DRIVE_PATHS`, `SEED`, `MT_N_RUNS`, `OCR_N_RUNS`) — never hardcoded.

Open your console and paste (one block at a time):

```bash
colab console -s goat
```

```bash
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
```

`git clone` pulls GitHub state, which does NOT include unpushed local work. Sync the 10 pipeline files from laptop to VM (repeat after every local edit):

```bash
colab upload -s goat model/notebooks/selection.sh /content/GOaT/model/notebooks/selection.sh
colab upload -s goat model/notebooks/selection.ipynb /content/GOaT/model/notebooks/selection.ipynb
colab upload -s goat model/notebooks/data/download_data.py /content/GOaT/model/notebooks/data/download_data.py
colab upload -s goat model/notebooks/selection/select_mt.py /content/GOaT/model/notebooks/selection/select_mt.py
colab upload -s goat model/notebooks/selection/select_ocr.py /content/GOaT/model/notebooks/selection/select_ocr.py
colab upload -s goat model/notebooks/training/train_mt.py /content/GOaT/model/notebooks/training/train_mt.py
colab upload -s goat model/notebooks/training/train_ocr.py /content/GOaT/model/notebooks/training/train_ocr.py
colab upload -s goat model/src/goat_model/mt/train.py /content/GOaT/model/src/goat_model/mt/train.py
colab upload -s goat model/src/goat_model/constants.py /content/GOaT/model/src/goat_model/constants.py
colab upload -s goat model/src/goat_model/ocr/train.py /content/GOaT/model/src/goat_model/ocr/train.py
```

(`upload` fails with 500 if the remote parent dir is missing — `mkdir -p` it in the console first.)

Then launch (paste in console). Close the console whenever — `nohup ... &` detaches and the keep-alive daemon holds the VM:

```bash
nohup bash /content/GOaT/model/notebooks/selection.sh > /tmp/goat_log.txt 2>&1 &
```

Check progress (fresh console, paste):

```bash
tail -c 2000 /tmp/goat_log.txt
```

Or without console: `colab log -s goat`. Re-running is safe — every step skips when its output JSON already exists, so no step burns GPU twice.

If any command ever fails with `module 'jupyter_kernel_client' has no attribute 'KernelClient'`, re-pin the dep (unpinned `jupyter-kernel-client` resolves to 1.x, which renamed the class `colab-cli 0.6.0` imports):
```bash
uv pip install --python ~/.local/share/uv/tools/google-colab-cli/bin/python "jupyter-kernel-client==0.15.0"
```

`notebooks/selection.sh` is the shell twin of `selection.ipynb` (same stem, same steps, same order, same args). Both call only step files with Drive args from constants:

```bash
uv run python notebooks/data/download_data.py --dataset scb-mt --out-dir "$DRIVE/datasets/mt"
uv run python notebooks/data/download_data.py --dataset flores200 --out-dir "$DRIVE/datasets/mt/test"
uv run python notebooks/data/download_data.py --dataset thaiocrbench --out-dir "$DRIVE/datasets/ocr/thaiocrbench"
uv run python notebooks/data/download_data.py --dataset thai-ocr-evaluation --out-dir "$DRIVE/datasets/ocr/thai-ocr-evaluation"
uv run python notebooks/selection/select_mt.py --mt-test-dir "$DRIVE/datasets/mt/test" --output "$DRIVE/results/mt_selection.json" --repeats "$REPEATS_MT" --seed "$SEED"
uv run python notebooks/selection/select_ocr.py --ocr-eval-dir "$DRIVE/datasets/ocr" --output "$DRIVE/results/ocr_selection.json" --repeats "$REPEATS_OCR" --seed "$SEED"
uv run python notebooks/training/train_mt.py --mt-dir "$DRIVE/datasets/mt" --selection "$DRIVE/results/mt_selection.json" --output "$DRIVE/results/mt_training.json" --out-root /content/artifacts/mt_lora --seed "$SEED"
uv run python notebooks/training/train_ocr.py --selection "$DRIVE/results/ocr_selection.json" --data-root "$DRIVE/data" --output "$DRIVE/results/ocr_training.json" --out-root /content/artifacts/ocr --seed "$SEED"
```

Results at `GOaT/results/` on Drive. Stop: `colab stop -s goat`.

## Way 2: VS Code (all from editor, click run)

1. Open `GOaT/` in VS Code
2. `Select Kernel` > `Colab` > `New Colab Server` > `T4`
3. Open `model/notebooks/selection.ipynb` > Run All

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
