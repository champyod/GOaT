# GOaT Colab — Selection & Training

## Selection (steps 6-8)
```bash
colab new -s goat --gpu T4
colab drivemount -s goat
colab console -s goat
```
```bash
export HF_TOKEN=hf_...
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
git -C /content/GOaT pull --ff-only
mkdir -p /content/drive/MyDrive/GOaT/logs
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader; ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1)'
```
Debug: full copy-paste with `--debug` — reaches all selection scripts, no manual runs needed (`GOAT_DEBUG=1` env works instead of the flag):
```bash
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_log.txt /content/drive/MyDrive/GOaT/logs/goat_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader; ps -o pcpu,pmem,etime,args -p $(pgrep -f "select_mt|select_ocr" | head -1)'
```
Isolated rerun of one script:
```bash
uv run python notebooks/selection/select_mt.py --mt-test-dir $MT_TEST_DIR --output $RESULTS/mt_selection.json --repeats $REPEATS_MT --seed $SEED --debug
```

## Training (steps 9-10)
```bash
nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader; ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -1)'
```
Debug: full copy-paste with `--debug` — reaches train_mt/train_ocr/generate_synthetic, no manual runs needed:
```bash
nohup bash /content/GOaT/model/notebooks/training.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_training_log.txt 2>&1 &
nohup bash -c 'while true; do cp /tmp/goat_training_log.txt /content/drive/MyDrive/GOaT/logs/goat_training_log.txt; sleep 300; done' > /dev/null 2>&1 &
watch -n 3 'tail -n 30 /tmp/goat_training_log.txt; echo ---; free -h | head -2; df -h / /tmp | tail -2; nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader; ps -o pcpu,pmem,etime,args -p $(pgrep -f "train_mt|train_ocr" | head -1)'
```
Isolated rerun of one script:
```bash
uv run python scripts/generate_synthetic.py --out $DATA_ROOT/synthetic --debug
```
# synth runs fully local: Drive gen/fonts/corpus copy to /tmp/synth_* first,
# workers read /tmp only, bg sync + final sync push results back to Drive.
# --debug forwards -v to synthtiger so per-sample tracebacks land in synth_gen.log.
# train_mt skips when mt_training.json already exists (already-trained guard).

## Common
- Resume: `git pull` pulls new code; partials resume same VM and new VM (Drive).
- Logs: `/tmp/*` fast, Drive `logs/` every 5m.
- Keep host: `while true; do echo "--- $(date) ---"; colab exec -s goat <<< "print('ping')" >/dev/null 2>&1; colab sessions; colab status -s goat 2>&1 | head -5; sleep 30; done`
