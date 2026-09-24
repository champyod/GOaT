# Run/grade split + replay + Drive export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split model inference (run) from scoring (grade) for OCR and MT, make any timestamped run re-gradable without a GPU, add a `--reverse` line-order flag (top-to-bottom default), mirror outputs to Drive via one `--mirror-dir` arg, and export trained models once (ThaiTrOCR→ONNX, NLLB→CT2 INT8) directly to Drive.

**Architecture:** `evaluate.run_ocr` / `evaluate.run_mt` become inference-only and dump raw timestamped `.run_<id>.jsonl` files; new `score_*` functions grade those rows (CER/BLEU + traces + verdicts). Selectors, replay CLIs, and trainers all consume the same two functions. Drive writes go through the existing `sync_dir` helper plus a new fail-loudly `require_drive` guard.

**Tech Stack:** Python 3.12 (uv), torch `==2.7.1`, transformers `==4.57.6`, onnxruntime, ctranslate2, existing `goat_model` package patterns (`log_call`, `write_json`, `LogProgress`).

**Spec:** User decisions from session (no separate spec file): split applies to OCR+MT; `--reverse` flag reorders hypothesis lines to match the answer key (default top-to-bottom, proven 0.849→0.054 on the 15.jpg fixture); single `--mirror-dir` arg reusing `sync_dir`; MT keeper = LoRA adapters only; one-shot export (ONNX for OCR, CTranslate2 INT8 for MT — best fit for the Rust backend) written directly to Drive, failing loudly when Drive is absent.

## Global Constraints

- Python 3.12 only (`uv python pin 3.12` in `model/`); never `pip install`, only `uv` (+ bun for `app/`, cargo for `src-tauri/`).
- transformers `==4.57.6`, torch `==2.7.1`, huggingface-hub `==0.36.0` stay pinned (`model/pyproject.toml`).
- CER stays `edit_distance / max(ref_len, hyp_len)`; `word_accuracy = 1 - CER`; Thai BLEU stays pythainlp-newmm segmented with `tokenize="none"`.
- Decision rules unchanged: OCR picks ThaiTrOCR iff mean CER ≤ 0.10 else lowest CER; MT keeps 600M iff BLEU > 35 AND ≤ 2 s; direction EN→TH.
- Default (no `--reverse`) scoring must be byte-identical to today's numbers; `--reverse` only reorders hypothesis lines.
- Comments explain why, not what; no dead code; Svelte/Rust untouched.
- Never commit weights, datasets, results, `model/.env`, or generated icons; CI parity (`bun check`, clippy, fmt, `uv run main.py`) must hold.

## Review Focus

- Reference strings with no line breaks vs hypotheses with `\n` separators: scorer must treat `\n` exactly as today by default (no silent separator collapsing).
- Replaying a run file written by an older schema (missing `domain`/`latency_ms` keys): scorer must default them, never crash.
- `--mirror-dir` pointing at an unmounted Drive path: must fail with a clear message naming the missing mount, never half-copy.
- Export on a machine without torch/ctranslate2/GPU: must raise naming the missing piece and the `uv sync --extra` that fixes it.
- MT export when the adapter dir is missing (training skipped/failed): must fail loudly, never export the base model as if it were fine-tuned.

---

## File map

- Modify `model/src/goat_model/ocr/evaluate.py` — add `infer_ocr_records` (raw, no scores) + `order_hypothesis` + `score_ocr_records`; keep `run_ocr` as a thin wrapper (backward compat).
- Modify `model/src/goat_model/mt/evaluate.py` — add `infer_mt_result` (hypotheses + latency, no BLEU) + `score_mt_result`; keep `run_mt` as a thin wrapper.
- Modify `model/src/goat_model/utils.py` — add `require_drive(path)` fail-loudly guard.
- Modify `model/notebooks/selection/select_ocr.py` — use split, write raw run file, add `--reverse` + `--mirror-dir`.
- Modify `model/notebooks/selection/select_mt.py` — use split, write raw run file, add `--mirror-dir`.
- Modify `model/scripts/eval_ocr.py` — add `--run-file` + `--reverse` replay mode.
- Modify `model/scripts/eval_mt.py` — add `--run-file` replay mode.
- Modify `model/scripts/export_models.py` — fill `export_onnx` (torch export + onnxruntime smoke) + `export_ct2` (TransformersConverter INT8 + smoke); both take `--dest` on Drive, guarded by `require_drive`.
- Modify `model/notebooks/training/train_ocr.py` + `model/notebooks/training/train_mt.py` — call export once at end (`--export-dest`, default Drive artifacts path); MT writes adapters-only keeper manifest.
- Create `model/tests/test_run_score.py` — all new behavior, TDD.
- Modify `model/tests/test_selection_integration.py` — stub new imports only if needed (split keeps old names, so likely untouched).

---

### Task 1: OCR run/grade split + line order

**Files:**
- Modify: `model/src/goat_model/ocr/evaluate.py`
- Test: `model/tests/test_run_score.py`

**Interfaces:**
- Consumes: `OCRBackend.recognize`, `read_gt`, `cer`, `word_accuracy`, `trace_cer`, `SEED`.
- Produces: `infer_ocr_records(backend, assets, img_size, seed=SEED) -> list[dict]` with keys `image/reference/hypothesis/latency_ms/domain` (NO `cer`/`word_accuracy` keys); `order_hypothesis(hypothesis, reverse=False) -> str` (`" ".join(reversed(lines))` when reverse else input unchanged); `score_ocr_records(records, reverse=False) -> list[dict]` (adds `cer`, `word_accuracy`, `trace`, `verdict` using `c.OCR_CER_THRESHOLD`); `run_ocr(...)` unchanged signature, implemented as score-then-infer wrapper.

- [ ] **Step 1: Write the failing test**

```python
def test_infer_records_carry_no_scores() -> None:
    recs = infer_ocr_records(FakeBackend("สวัสดี"), FakeAssets(), 64)
    assert recs[0]["hypothesis"] == "สวัสดี"
    assert "cer" not in recs[0] and "word_accuracy" not in recs[0]


def test_reverse_reorders_glued_lines() -> None:
    hyp = "L1 text\nL2 text"
    assert order_hypothesis(hyp, reverse=False) == hyp
    assert order_hypothesis(hyp, reverse=True) == "L2 text L1 text"


def test_score_adds_trace_and_verdict() -> None:
    scored = score_ocr_records([{"image": "a.png", "reference": "ทองเนื้อเก้า", "hypothesis": "ทองเนื้อเก้า", "latency_ms": 1.0, "domain": None}])
    assert scored[0]["cer"] == 0.0 and scored[0]["verdict"] == "pass"
    assert scored[0]["trace"]["distance"] == 0
```

(FakeBackend/FakeAssets are 10-line test doubles defined at the top of the test file: `recognize` returns `OCRResult(text=..., latency_ms=1.0)`; assets expose `.image` (a real tiny png written to tmp) and `.gt` (a real tmp txt file) — `read_gt` needs real files.)

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_CACHE_DIR=/tmp/uv-cache PYTHONPATH=src uv run --isolated --with pytest --with "numpy>=1.26,<2" --with "scipy>=1.13" --with "sacrebleu>=2.4" python -m pytest tests/test_run_score.py -q`
Expected: FAIL with "cannot import name 'infer_ocr_records'".

- [ ] **Step 3: Write minimal implementation**

In `evaluate.py`, move the loop body of `run_ocr` into `infer_ocr_records` (drop the `cer(...)`/`word_accuracy(...)` calls and the metrics import), add:

```python
def order_hypothesis(hypothesis: str, reverse: bool = False) -> str:
    """Put hypothesis lines in scoring order.

    The gt answer key is one space-glued string with no line breaks, so the
    hypothesis must be glued the same way. Default keeps today's exact bytes
    (byte-identical scores); reverse flips top-to-bottom lines to bottom-up.
    """
    if not reverse:
        return hypothesis
    return " ".join(reversed(hypothesis.splitlines()))


def score_ocr_records(records: list[dict], reverse: bool = False) -> list[dict]:
    from goat_model import constants as c
    from goat_model.metrics import cer, trace_cer, word_accuracy

    scored: list[dict] = []
    for rec in records:
        hyp = order_hypothesis(rec["hypothesis"], reverse=reverse)
        ref = rec["reference"]
        scored.append({**rec, "hypothesis": hyp, "cer": cer(ref, hyp),
                       "word_accuracy": word_accuracy(ref, hyp),
                       "trace": trace_cer(ref, hyp),
                       "verdict": "pass" if cer(ref, hyp) <= c.OCR_CER_THRESHOLD else "fail"})
    return scored
```

and reimplement `run_ocr` as `return score_ocr_records(infer_ocr_records(backend, assets, img_size, seed=seed))`.

- [ ] **Step 4: Run tests to verify they pass**

Run: same pytest command as Step 2, plus the full suite `python -m pytest tests/ -q`.
Expected: all PASS (32 existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add model/src/goat_model/ocr/evaluate.py model/tests/test_run_score.py
git commit -m "split ocr run and grade"
```

### Task 2: MT run/grade split

**Files:**
- Modify: `model/src/goat_model/mt/evaluate.py`
- Test: `model/tests/test_run_score.py` (append)

**Interfaces:**
- Consumes: `MTBackend.translate`, `corpus_bleu`.
- Produces: `infer_mt_result(backend, sources, batch_size=16, seed=SEED) -> dict` with keys `hypotheses/per_batch_ms` (NO `bleu` key); `score_mt_result(refs, hypotheses, per_batch_ms, n_tokens_fn) -> dict` with keys `bleu/throughput_tokens_per_s/total_latency_ms/average_ms_per_sentence/hypotheses`; `run_mt(...)` unchanged signature, wrapper.

- [ ] **Step 1: Write the failing test**

```python
def test_infer_mt_has_no_bleu() -> None:
    res = infer_mt_result(FakeMT(["h1", "h2"]), ["s1", "s2"])
    assert res["hypotheses"] == ["h1", "h2"]
    assert "bleu" not in res


def test_score_mt_matches_run_mt_shape() -> None:
    res = score_mt_result(["r1", "r2"], ["h1", "h2"], [10.0, 20.0], lambda h: 4)
    assert set(res) == {"bleu", "throughput_tokens_per_s", "total_latency_ms", "average_ms_per_sentence", "hypotheses"}
```

(FakeMT returns `MTResult(translations=[...], latency_ms=5.0)` from `translate` and `4` from `n_tokens`.)

- [ ] **Step 2: Run test to verify it fails**

Run: same pytest command as Task 1 Step 2.
Expected: FAIL with "cannot import name 'infer_mt_result'".

- [ ] **Step 3: Write minimal implementation**

Move the batch loop of `run_mt` into `infer_mt_result` (return `{"hypotheses": hypotheses, "per_batch_ms": per_batch_ms}`), move the BLEU/latency math into `score_mt_result(refs, hypotheses, per_batch_ms, n_tokens_fn)` where `n_tokens_fn` is `backend.n_tokens` passed in (keeps evaluate importable without backends), reimplement `run_mt` as infer-then-score wrapper returning the identical dict shape.

- [ ] **Step 4: Run tests to verify they pass**

Run: full suite as Task 1 Step 4.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add model/src/goat_model/mt/evaluate.py model/tests/test_run_score.py
git commit -m "split mt run and grade"
```

### Task 3: Drive guard + selector wiring (`--reverse`, `--mirror-dir`, raw run files)

**Files:**
- Modify: `model/src/goat_model/utils.py` (add `require_drive`)
- Modify: `model/notebooks/selection/select_ocr.py`
- Modify: `model/notebooks/selection/select_mt.py`
- Test: `model/tests/test_run_score.py` (append: `require_drive` raises on missing path, passes on existing; `sync_dir` copies new files — both with tmp dirs, no Drive needed)

**Interfaces:**
- Consumes: `sync_dir`, `infer_*/score_*` from Tasks 1–2.
- Produces: `require_drive(path: Path) -> Path` (returns path if its mount root exists, else `raise SystemExit(f"Drive not mounted: ... — mount it, then retry")`); selectors accept `--reverse` (OCR only, passed to `score_ocr_records`) and `--mirror-dir` (both; after each file write, `sync_dir(output.parent glob of run files, mirror_dir)`).

- [ ] **Step 1: Write the failing test**

```python
def test_require_drive_fails_loudly(tmp_path) -> None:
    with pytest.raises(SystemExit, match="Drive not mounted"):
        require_drive(tmp_path / "nope" / "x.json")


def test_require_drive_passes_on_local(tmp_path) -> None:
    p = tmp_path / "f.json"
    p.write_text("{}")
    assert require_drive(p) == p
```

- [ ] **Step 2: Run test to verify it fails**

Run: same pytest command.
Expected: FAIL with "cannot import name 'require_drive'".

- [ ] **Step 3: Write minimal implementation**

`utils.py`:

```python
def require_drive(path: Path) -> Path:
    """Fail loudly when a Drive destination is not mounted.

    Local dev runs have no /content/drive; silently writing "to Drive" would
    lose exports, so refuse instead of guessing.
    """
    p = Path(path)
    if p.exists() or p.parent.exists():
        return p
    raise SystemExit(f"Drive not mounted: {p} — mount Drive, then retry")
```

Selectors: replace the `evaluate.run_ocr(...)` call with `infer` → append raw rows (repeat/model/dataset/image/reference/hypothesis/latency/domain, no scores) to `output.with_name(f"{stem}.run_{run_id}.jsonl")` → `score_ocr_records(recs, reverse=args.reverse)` for stats; same shape for MT with `infer_mt_result`/`score_mt_result` (no `--reverse` on MT); after every `write_json`/dump, `if args.mirror_dir: sync_dir(args.output.parent, args.mirror_dir)` guarded by `require_drive(args.mirror_dir)` once at startup.

- [ ] **Step 4: Run tests + py_compile selectors, verify full suite passes**

Run: full pytest suite; `python3 -m py_compile notebooks/selection/select_ocr.py notebooks/selection/select_mt.py`.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add model/src/goat_model/utils.py model/notebooks/selection/select_ocr.py model/notebooks/selection/select_mt.py model/tests/test_run_score.py
git commit -m "wire run files reverse mirror"
```

### Task 4: Replay CLIs (`--run-file`)

**Files:**
- Modify: `model/scripts/eval_ocr.py` (add `--run-file` + `--reverse` + `--output`)
- Modify: `model/scripts/eval_mt.py` (add `--run-file` + `--output`)
- Test: `model/tests/test_run_score.py` (append: build a 2-row raw run file in tmp, run `score_ocr_records` on its rows, assert scored CER matches direct `cer()` — pins replay-without-GPU)

- [ ] **Step 1: Write the failing test**

```python
def test_replay_scores_saved_rows(tmp_path) -> None:
    run_file = tmp_path / "x.run_20240101T000000Z.jsonl"
    run_file.write_text(json.dumps({"image": "a.png", "reference": "abc", "hypothesis": "abc", "latency_ms": 1.0, "domain": None}) + "\n")
    rows = [json.loads(l) for l in run_file.read_text().splitlines()]
    assert score_ocr_records(rows)[0]["cer"] == cer("abc", "abc") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: same pytest command (passes already if Tasks 1–3 done — this pins the replay contract, so implement the CLI flag wiring next regardless).

- [ ] **Step 3: Write minimal implementation**

`eval_ocr.py`: `--run-file` mode reads JSONL rows, calls `score_ocr_records(rows, reverse=args.reverse)`, aggregates with `evaluate.aggregate_records`, writes `--output`. No-backend path imports neither paddle nor torch. Mirror `--run-file` for MT via `score_mt_result` + existing aggregate shape.

- [ ] **Step 4: Run tests + py_compile both scripts**

Run: full pytest suite; `python3 -m py_compile scripts/eval_ocr.py scripts/eval_mt.py`.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add model/scripts/eval_ocr.py model/scripts/eval_mt.py model/tests/test_run_score.py
git commit -m "add replay by run file"
```

### Task 5: One-shot export to Drive (ONNX + CT2) + MT adapters keeper

**Files:**
- Modify: `model/scripts/export_models.py`
- Modify: `model/notebooks/training/train_ocr.py` + `model/notebooks/training/train_mt.py` (call export once at end; `--export-dest` defaulting to Drive artifacts path)
- Test: `model/tests/test_run_score.py` (append: `export_onnx(None)` raises RuntimeError naming the missing artifact; `require_drive` on missing mount raises SystemExit — no GPU/torch needed)

**Interfaces:**
- Consumes: `require_drive`, `write_json`, `c.ART_OCR`/`c.ART_MT`.
- Produces: `export_onnx(src_dir, dest_dir) -> Path` (torch `onnx.export` of the fine-tuned ThaiTrOCR with dummy 384px pixels, opset pinned in code comment, onnxruntime smoke load + one dummy infer); `export_ct2(src_dir, dest_dir) -> Path` (`TransformersConverter.convert` + INT8 quantize + one-sentence smoke translate); train wrappers pass `--export-dest` (default `DRIVE_ROOT/artifacts/...`) and MT writes `adapters/` + `keeper.json` (base id, adapter files, grid winner settings, final BLEU).

- [ ] **Step 1: Write the failing test**

```python
def test_export_refuses_missing_artifact(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="run scripts/train_ocr.py first"):
        export_onnx(None)
    with pytest.raises(SystemExit, match="Drive not mounted"):
        require_drive(tmp_path / "drive" / "artifacts")
```

- [ ] **Step 2: Run test to verify it fails**

Run: same pytest command.
Expected: FAIL with "cannot import name 'export_onnx'" (import from `scripts.export_models` via path insert in the test, mirroring `test_selection_integration.py` stub style).

- [ ] **Step 3: Write minimal implementation**

Fill both exporters (single call each — no per-epoch logic; trainers keep their existing per-epoch checkpoint mirroring untouched). Wrappers: after `run_*_finetune` returns success, call the exporter once with `require_drive(args.export_dest)`; MT wrapper additionally copies `adapter_model.*` + writes `keeper.json` via `write_json`.

- [ ] **Step 4: Run tests (export GPU paths untested locally — smoke code py_compiles) + ruff**

Run: full pytest suite; `python3 -m py_compile scripts/export_models.py notebooks/training/train_ocr.py notebooks/training/train_mt.py`; `ruff check src/ tests/`.
Expected: all PASS, ruff clean on touched files.

- [ ] **Step 5: Commit**

```bash
git add model/scripts/export_models.py model/notebooks/training/train_ocr.py model/notebooks/training/train_mt.py model/tests/test_run_score.py
git commit -m "export once to drive"
```

---

## Self-review

- Spec coverage: split (Tasks 1–2) ✓, replay by timestamp (Task 4) ✓, `--reverse` default top-to-bottom + select/train hardcoded default (Tasks 1, 3) ✓, `--mirror-dir` single-arg least-change mirroring (Task 3) ✓, MT adapters-only keeper (Task 5) ✓, one-shot ONNX/CT2 export direct to Drive failing loudly (Task 5) ✓.
- Placeholder scan: every step names exact files, signatures, commands, and expected outputs; no TBD/TODO.
- Type consistency: raw rows always carry `image/reference/hypothesis/latency_ms/domain`; scored rows add `cer/word_accuracy/trace/verdict` (OCR) — same keys the selectors already log today, so downstream snapshots don't change shape.
- Review Focus mapping: separator handling (Task 1 `order_hypothesis` default-unchanged + test) ✓; old-schema replay (score functions use `rec.get("domain")`/`rec.get("latency_ms", 0.0)` defaults — add to Task 1 Step 3) ✓; unmounted mirror (Task 3 `require_drive` test) ✓; missing export deps (Task 5 tests) ✓; missing adapter dir (exporter `is_dir` check raising, Task 5) ✓.
- One fix applied inline: Task 1 scoring uses `.get` defaults for replay robustness.
