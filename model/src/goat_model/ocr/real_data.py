"""Dataset side of the real-screenshot CPU judge: fetch + layout guard.

`fetch_dataset` snapshots the HF dataset; `discover_assets` turns a local dir into
the one shared, sorted image list that every model walks and every paired test
re-joins on. An image with no key file stays in the list as inference-only:
dropping it would shrink a model's image set and break the paired tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from goat_model.log import info as _info
from goat_model.ocr.evaluate import IMG_EXTS

TAG = "judge-real-cpu"


@dataclass(frozen=True)
class Asset:
    """One screenshot and its key file, or `None` when the image has no key."""

    image: Path
    gt: Path | None


def fetch_dataset(repo_id: str, local_dir: Path, token: str | None) -> Path:
    """Snapshot an HF dataset into `local_dir`. The token is never logged."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as err:
        raise RuntimeError("huggingface_hub not installed - run `uv sync --extra ocr`") from err
    _info(TAG, "downloading dataset", repo=repo_id, out=str(local_dir))
    snapshot_download(repo_id=repo_id, repo_type="dataset", local_dir=str(local_dir), token=token)
    return local_dir


def _image_dir(root: Path) -> Path:
    return root / "images" if (root / "images").is_dir() else root


def _image_files(directory: Path, recursive: bool = False) -> list[Path]:
    """Image files inside `directory`, or anywhere below it when recursive."""
    if not directory.is_dir():
        return []
    paths = directory.rglob("*") if recursive else directory.iterdir()
    return [path for path in paths if path.is_file() and path.suffix.lower() in IMG_EXTS]


def has_images(root: Path) -> bool:
    """True when `discover_assets` would find a dataset here - the same rule, so
    the CLI neither skips a download it holds nor downloads over one it holds."""
    return bool(_image_files(_image_dir(root)))


def unsupported_layout(root: Path) -> str | None:
    """Why `discover_assets` would refuse `root`, or None when it would not.

    A nested split dir (`images/train/`) is named explicitly, because the fix is
    a re-download, never a skip that crashes later on an empty image list.
    """
    if has_images(root):
        return None
    below = sorted({str(p.parent.relative_to(root)) for p in _image_files(root, recursive=True)})
    found = f"image files only below {below}" if below else "no image files at all"
    return (
        f"unsupported dataset layout under {root}: {found}. Expected image files directly "
        f"under {root}/images/ or directly under {root}, with keys in {root}/gt/. "
        f"Re-download into an empty dir: rm -rf {root}, then rerun this command."
    )


def _gt_path(image: Path, root: Path) -> Path | None:
    """The key file: `root/gt/<stem>.txt` first, then a `gt/` beside the image."""
    for gt_dir in (root / "gt", image.parent / "gt"):
        if (candidate := gt_dir / f"{image.stem}.txt").is_file():
            return candidate
    return None


def discover_assets(root: Path) -> list[Asset]:
    """Flat (`root/*.png` + `root/gt/*.txt`) or `images/` + `gt/` layout, sorted
    by file name: the parent and every worker must agree on the order. An image
    with no key stays in the list as inference-only - dropping it would shrink a
    model's image set and break the paired tests.
    """
    assets = [Asset(image=path, gt=_gt_path(path, root)) for path in _image_files(_image_dir(root))]
    if not assets:
        raise FileNotFoundError(unsupported_layout(root))
    return sorted(assets, key=lambda asset: asset.image.name)
