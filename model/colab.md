# GOaT Colab — Selection & Training

## Selection (steps 6-8)
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
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1)'
```

### Selection debug
Same commands with `--debug` on the script call — the flag fans out to every python step (per-action enter/exit, full args, tracebacks). `GOAT_DEBUG=1` works instead of the flag.
```bash
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1)'
```
Isolated single-script rerun:
```bash
uv run python notebooks/selection/select_mt.py --mt-test-dir $MT_TEST_DIR --output $RESULTS/mt_selection.json --repeats $REPEATS_MT --seed $SEED --debug
```

## Training (steps 9-10)
```bash
nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 25 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -5)'
```

### Training debug
Same commands with `--debug` on the script call — reaches train_mt, train_ocr and generate_synthetic.
```bash
nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 25 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; (nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null || echo "no gpu"); ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -5)'
```
Isolated single-script rerun:
```bash
uv run python scripts/generate_synthetic.py --out /tmp/goat_synth_data/synthetic --debug
```
Notes:
- generate_synthetic downloads the 10k SynthTIGER images from the HF dataset (KunanonKhai/Synthetic-GOaT-OCR), flattens them, then 70/15/15 splits into /tmp/goat_synth_data/{train,val,test} for train_ocr.
- Synth never touches Drive (local-only, so log/result syncing never contends).
- train_mt skips when mt_training.json already exists (already-trained guard).

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
- Keep host: `while true; do echo "--- $(date) ---"; colab exec -s goat <<< "print('ping')" >/dev/null 2>&1; colab sessions; colab status -s goat 2>&1 | head -5; sleep 30; done`
