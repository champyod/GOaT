"""Raw record + resume checkpoint for the real-screenshot CPU judge.

A 500-image pass per model is long enough that a dead session must not throw
away the records it already produced, so every scored image reaches disk the
moment it exists. This module owns the two files that make that survivable,
both following the selection convention (notebooks/selection/select_ocr.py):

    <output>.partial.json             the resume checkpoint, per model and repeat
    <output>.samples_<runid>.jsonl    the append-only raw record, one line per image

Ordering is the whole contract: the sample log is appended and flushed BEFORE
the checkpoint is extended, so a crash can leave the checkpoint behind the data
- one image re-scored - but never ahead of it, which would silently skip an
image. The sample log is therefore the authority on what has been done and the
checkpoint is only the cheap summary of it, which is why an unreadable
checkpoint costs a re-run and never a record.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, TextIO

from goat_model.log import error as _err
from goat_model.log import info as _info
from goat_model.log import warning as _warn

TAG = "judge-real-cpu"

try:
    import fcntl
except ImportError:  # non-POSIX: no advisory lock, see _locked()
    fcntl = None


def partial_path(output: Path) -> Path:
    """`<output>.partial.json`: the resume checkpoint."""
    return output.with_name(output.stem + ".partial.json")


def samples_path(output: Path, run_id: str) -> Path:
    """`<output>.samples_<runid>.jsonl`: the append-only raw record."""
    return output.with_name(f"{output.stem}.samples_{run_id}.jsonl")


def new_run_id() -> str:
    """Sortable UTC stamp; the samples file name carries it so runs never collide.

    Microsecond resolution, not seconds: --force has to land in a *different*
    log, and reusing the previous run's log would let the append guard skip
    every image and turn a forced re-score into a silent no-op.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def load_partial(path: Path, seed: int, repeats: int) -> dict:
    """The checkpoint to resume from, or {} when there is nothing to resume.

    A checkpoint recorded under another seed or repeat count describes a
    different experiment, so it is ignored rather than mixed into this report -
    the same rule select_ocr._load_partial applies.
    """
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        _info(TAG, "checkpoint unreadable, starting fresh", path=str(path), error=str(err))
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("run_id"), str):
        _err(TAG, "checkpoint malformed, starting fresh", path=str(path))
        return {}
    if doc.get("seed") != seed or doc.get("repeats") != repeats:
        _info(TAG, "checkpoint is another configuration, starting fresh", path=str(path),
              seed=doc.get("seed"), repeats=doc.get("repeats"))
        return {}
    return doc


def start_partial(path: Path, run_id: str, seed: int, repeats: int) -> None:
    """Seed an empty checkpoint before the workers start, so a run that dies in
    its first seconds still names the samples file the resume has to append to."""
    write_json_atomic(path, {"run_id": run_id, "seed": seed, "repeats": repeats, "models": {}})


def write_json_atomic(path: Path, doc: dict) -> None:
    """Write through a sibling temp file and rename.

    The checkpoint is rewritten once per image, so a plain truncating write hit
    by a kill would drop every model's progress in one stroke; rename is atomic,
    which leaves at worst the previous checkpoint in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock across a checkpoint read-modify-write.

    Every model runs in its own process, so two workers reaching the doc at the
    same time would otherwise overwrite each other's model key and both would
    re-score images the other had already recorded. flock is released by the
    kernel when its holder dies, so a killed worker cannot leave the next one
    waiting on a stale lock.
    """
    lock = path.with_name(f"{path.name}.lock")
    handle: TextIO = lock.open("a", encoding="utf-8")
    try:
        if fcntl is None:
            _warn(TAG, "no advisory lock here, checkpoint merge unprotected", path=str(lock))
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _read_json(path: Path) -> dict:
    """The checkpoint as a dict, or an empty one.

    A missing, half-written or malformed file rebuilds as empty on purpose: the
    sample log still holds every record, so the cost is a re-score, whereas
    trusting a damaged doc could skip an image that was never scored.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        _warn(TAG, "checkpoint unreadable, rebuilding it", path=str(path), error=str(err))
        return {}
    if not isinstance(doc, dict):
        _warn(TAG, "checkpoint is not an object, rebuilding it", path=str(path))
        return {}
    return doc


def checkpoint(partial: Path, model: str, repeat_index: int, stems: Sequence[str]) -> None:
    """Merge one worker's scored stems into the shared checkpoint.

    Called only after the matching records are in the sample log, and holding
    the lock, so the checkpoint is a conservative view of what is on disk.
    """
    with _locked(partial):
        doc = _read_json(partial)
        doc.setdefault("models", {}).setdefault(model, {})[str(repeat_index)] = list(stems)
        write_json_atomic(partial, doc)


def partial_stems(partial: Path, model: str) -> dict[int, list[str]]:
    """What the checkpoint claims for one model, per repeat index."""
    repeats = _read_json(partial).get("models", {}).get(model, {})
    if not isinstance(repeats, dict):
        _err(TAG, "checkpoint model entry malformed", path=str(partial), model=model)
        return {}
    return {int(key): list(value) for key, value in repeats.items() if isinstance(value, list)}


def read_samples(path: Path | None) -> list[dict]:
    """Every record in the raw log, in file order; a broken line fails loud.

    A line is only ever appended whole (one write of a short line, then flush),
    so an unparsable line means the file itself is damaged, not that a worker
    was interrupted mid-line - guessing which record survived would silently
    change the judge's numbers.
    """
    if path is None or not path.is_file():
        return []
    rows: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as err:
            raise ValueError(f"{path}:{number} is not a record line: {err}") from err
        if not isinstance(row, dict):
            raise TypeError(f"{path}:{number} is a {type(row).__name__}, expected an object")
        rows.append(row)
    return rows


def index_rows(rows: list[dict]) -> dict[str, dict[int, dict[str, dict]]]:
    """rows -> `{model: {repeat_index: {image: row}}}`.

    A repeated (model, repeat, image) means the append guard failed; the judge
    must not average one screenshot twice, so a duplicate is an error here
    rather than a silent overwrite.
    """
    out: dict[str, dict[int, dict[str, dict]]] = {}
    for row in rows:
        bucket = out.setdefault(str(row["model"]), {}).setdefault(int(row["repeat_index"]), {})
        image = str(row["image"])
        if image in bucket:
            raise ValueError(
                f"duplicate record: {row['model']} repeat {row['repeat_index']} image {image}"
            )
        bucket[image] = row
    return out


def runs_from_rows(rows: list[dict], order: Sequence[str]) -> dict[str, list[list[dict]]]:
    """The raw log shaped like the report: `{model: [run per repeat]}`, each run
    in `order` (the shared asset list).

    Order comes from the asset list rather than the file, because the workers
    append concurrently: a resumed run and a fresh one must group identically
    even when their lines interleave differently.
    """
    index = {name: position for position, name in enumerate(order)}
    out: dict[str, list[list[dict]]] = {}
    for model, repeats in index_rows(rows).items():
        runs = []
        for _, by_image in sorted(repeats.items()):
            names = sorted(by_image, key=lambda name: index.get(name, len(index)))
            runs.append([by_image[name] for name in names])
        out[model] = runs
    return out


class SampleLog:
    """Append-only JSONL sink for one worker's records: one flushed line each.

    Opened before any weights load, so a path that cannot be written fails the
    model outright instead of dropping a scored image at the end of the pass.
    Flushed per record rather than fsynced: a session death - not a power cut -
    is the failure this guards against, and the kernel keeps flushed bytes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, row: dict) -> None:
        if self._handle is None:
            raise RuntimeError(f"sample log {self.path} is closed: {row.get('image')} dropped")
        self._handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class Recorder:
    """The write path for one model's pass: sample log first, checkpoint after.

    `take` is the resume guard. It answers from the rows already in the log, so
    a second start - or a start after a kill - cannot score or append an image
    that is already recorded, no matter how many times the process is restarted.
    """

    def __init__(self, model: str, repeats: int, samples: Path, partial: Path,
                 log: SampleLog) -> None:
        self.model = model
        self._partial = partial
        self._log = log
        prior = index_rows(read_samples(samples)).get(model, {})
        self._prior = prior
        # Seeded from the log, not from zero: the checkpoint must keep listing the
        # images an earlier process recorded, or the merge would drop them.
        self._stems = {index: list(prior.get(index, {})) for index in range(repeats)}
        self.resumed = sum(len(images) for images in prior.values())

    def take(self, repeat_index: int, image: str) -> dict | None:
        """The recorded row for an image this pass has already scored, else None."""
        return self._prior.get(repeat_index, {}).get(image)

    def commit(self, row: dict) -> None:
        """Append the record, then extend the checkpoint - never the other way.

        Per image rather than every N: the doc costs roughly 60 bytes per stem,
        so a 500-image pass writes a few MB against hours of inference, and
        per-image checkpointing is what caps a crash at one repeated image.
        """
        self._log.append(row)
        repeat_index = int(row["repeat_index"])
        self._stems[repeat_index].append(str(row["image"]))
        checkpoint(self._partial, self.model, repeat_index, self._stems[repeat_index])
