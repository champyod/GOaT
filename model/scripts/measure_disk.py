#!/usr/bin/env python3
"""Disk footprint of an installed GOaT app: three numbers over an enumerated install set.

One shell `du -sh` answers "how big is this directory" for one directory at one moment and
leaves nothing to check: no file list, no digest, no record of which directories it covered. A
Tauri app installs to more than one place - the executable's directory, the resource directory,
the app data directory, the cache directory, an OCR sidecar and a desktop entry - so a figure
quoted from one of them is a figure for that directory and not for the app. This enumerates the
install set first, then reports three numbers per location and per install set:

* `apparent_bytes` - sum of `st_size` over every non-directory entry walked, what
  `du --apparent-size` reports and what a file manager shows. A directory's `st_size` is the size
  of the directory entry, a few dozen bytes that grow with its children's names, and du leaves
  those out, so they are left out here too and a du figure is comparable without adjustment.
* `allocated_bytes` - sum of `st_blocks * 512`, what du reports and the filesystem reserved.
  Directory inodes do hold blocks and are counted here. Sparse and hard-linked files move the two
  figures apart as well, so both are reported and neither is derived from the other.
* `dpkg_installed_size_bytes` - only when the artefact is a `.deb`, from its own control fields.

Every file is listed with its two byte figures and its sha256, so a total can be re-derived from
the list beside it and a later claim of "the same artefact" is checked against digests. A walked
directory's own inode is not in that list - `os.walk` names the root but yields its children -
so its two byte figures are recorded per role under `walk_root`, which is what makes a du figure
over the same path reconcilable instead of merely close. That reconciliation is exact for a role
whose entries all sit under the walk root. `executable_dir` is not such a role: it lists the
executable itself beside the directory it resolves into, so a link outside that directory puts its
own `st_size` into the role's apparent total and into no du figure taken over the walked path. Its
`allocated_bytes` is absent from that figure the same way on a filesystem that gives a symlink
blocks of its own, and absent from nothing on one that stores a fast link inside its inode, so the
allocated identity for a symlinked executable is filesystem dependent rather than exact.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goat_model.log import info as _info
from goat_model.log import warning as _warn
from goat_model.utils import log_call, write_json

#: `st_blocks` counts 512-byte units on every filesystem, independent of that
#: filesystem's own block size, so this constant is what makes `allocated_bytes`
#: comparable across the install set rather than only within one filesystem.
BLOCK_BYTES = 512

HASH_CHUNK_BYTES = 4 * 1024 * 1024
APP_IDENTIFIER = "com.goat.app"
DPKG_TIMEOUT_S = 30
DEFAULT_OUTPUT = Path("results/disk.json")

SCHEMA = "goat-disk-footprint/1"

#: The roles without which there is no install set to report: the artefact as distributed, the
#: directory the installed executable lives in, and the directory the bundle's resources land in.
#: A missing sidecar or desktop entry is a fact about that install and is recorded, not an error.
REQUIRED_ROLES = ("artefact", "executable_dir", "resource_dir")

#: Directories a Linux distribution installs a package's executable into. The installed
#: executable is normally a symlink from one of these into the bundle, so walking the link's own
#: parent would charge every other binary on the host to this app.
SYSTEM_BINARY_DIRS = frozenset((
    "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin",
))


def _relative_to_root(path: Path, root: Path) -> str:
    """`path` as the file list writes it: relative to the walk root, absolute when outside it.

    The installed executable is listed beside the directory it resolves into, so it can sit
    outside that directory, and a relative path is only meaningful within a root.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _kind(st: os.stat_result) -> str:
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    return "other"


def _lstat(path: Path) -> tuple[os.stat_result | None, str]:
    """`lstat` of a path, or the reason there is none.

    Every walk uses `lstat` rather than `stat`: following a symlink would charge
    the walk for a target tree that the install set already reaches by its own
    path, or that lies outside the app entirely.
    """
    try:
        return path.lstat(), ""
    except OSError as exc:
        return None, f"lstat failed: {exc}"


def _sha256(path: Path) -> tuple[str | None, str]:
    """Content digest of a regular file, or the reason there is none.

    A file that cannot be read leaves the digest null beside the reason and is
    counted in the byte totals anyway, because a size the kernel reports and a
    digest it refuses are independent facts.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        return None, f"sha256 could not be read: {exc}"
    return digest.hexdigest(), ""


def _entry_record(path: Path, root: Path, st: os.stat_result) -> dict:
    """One walked entry: its two byte figures, its inode, and its digest.

    A symlink's `st_size` is the length of the target path and its blocks are the
    link's own, so a symlink contributes a few bytes rather than the tree it
    points at. That is why the digest is null for it: there is no content here to
    hash.
    """
    record = {
        "abs_path": str(path),
        "path": _relative_to_root(path, root),
        "kind": _kind(st),
        "dev": st.st_dev,
        "ino": st.st_ino,
        "nlink": st.st_nlink,
        "apparent_bytes": st.st_size,
        "allocated_bytes": st.st_blocks * BLOCK_BYTES,
        "sha256": None,
        "error": None,
    }
    if record["kind"] == "file":
        record["sha256"], record["error"] = _sha256(path)
    return record


def _mark_hardlinks(entries: list[dict]) -> None:
    """Count each inode once, marking the later entries rather than dropping them.

    du counts a hard-linked file once per inode, so a walk that charged every
    directory entry would report more than the filesystem holds. The duplicates
    stay in the file list with `counted_in_totals` false and a pointer to the entry
    carrying them, which is what keeps the totals re-derivable from the list.
    """
    first_seen: dict[tuple[int, int], str] = {}
    for entry in entries:
        if entry["kind"] != "file" or entry["nlink"] <= 1:
            entry["counted_in_totals"] = True
            entry["duplicate_of"] = None
            continue
        inode = (entry["dev"], entry["ino"])
        if inode in first_seen:
            entry["counted_in_totals"] = False
            entry["duplicate_of"] = first_seen[inode]
            continue
        first_seen[inode] = entry["abs_path"]
        entry["counted_in_totals"] = True
        entry["duplicate_of"] = None


def _apparent_total(counted: list[dict]) -> int:
    """The apparent figure for a set of entries, counted the way du counts it.

    A directory's `st_size` is the size of the directory entry itself - a few dozen bytes that
    grow with the length of its children's names - not the size of what the directory holds. du
    excludes those bytes from its apparent total, so a walk that included them could not be
    compared with a du figure without first subtracting every directory in it. Their blocks are
    real and stay in the allocated total.
    """
    return sum(entry["apparent_bytes"] for entry in counted if entry["kind"] != "dir")


def _dedupe_paths(entries: list[dict]) -> None:
    """Drop the second record of an absolute path already in the list.

    The executable is listed explicitly and normally also appears in the directory walked beside
    it. One record per path is what keeps `_install_set` from counting that path twice, and
    `_mark_hardlinks` runs after this so the inode bookkeeping sees the deduplicated list.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        if entry["abs_path"] in seen:
            continue
        seen.add(entry["abs_path"])
        unique.append(entry)
    entries[:] = unique


def _walk(root: Path) -> tuple[list[dict], list[dict]]:
    """Every entry under `root`, in a deterministic order, with the errors that
    interrupted it.

    Names are sorted because an unsorted walk emits a different file list on every
    filesystem, and two runs of the same tree are then not comparable line for
    line. Symlinked directories are not descended into, which is du's default.
    """
    entries: list[dict] = []
    errors: list[dict] = []

    def _on_error(exc: OSError) -> None:
        errors.append({"path": exc.filename, "reason": str(exc)})

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_error, followlinks=False):
        for name in sorted(filenames) + sorted(dirnames):
            child = Path(dirpath) / name
            st, reason = _lstat(child)
            if st is None:
                errors.append({"path": str(child), "reason": reason})
                continue
            entries.append(_entry_record(child, root, st))
    return entries, errors


def _walk_root_block(path: Path, kind: str, measured: bool) -> dict | None:
    """The root of a walked directory, held aside because the walk does not list it.

    du's allocated total counts the starting directory's own blocks and its apparent total does
    not count the starting directory's own size. Both figures are recorded here so a du figure
    over the same path reconciles against this one by one inode instead of by an unexplained
    difference. Null for a role that is a file, where the path is itself the listed entry.
    """
    if kind != "directory" or not measured:
        return None
    st, reason = _lstat(path)
    if st is None:
        return {"path": str(path), "kind": None, "apparent_bytes": None,
                "allocated_bytes": None, "reason": reason}
    return {"path": str(path), "kind": _kind(st), "apparent_bytes": st.st_size,
            "allocated_bytes": st.st_blocks * BLOCK_BYTES, "reason": None}


def _location_block(role: str, path: Path, resolved_from: str, kind: str, entries: list[dict],
                    errors: list[dict], measured: bool, reason: str | None) -> dict:
    """One enumerated location: its counters, its two byte figures and its file list.

    An absent location keeps every counter at zero and `measured` false with its reason, rather
    than a null total a reader cannot tell from a directory that is genuinely empty.
    """
    counted = [entry for entry in entries if entry.get("counted_in_totals", True)]
    kinds = Counter(entry["kind"] for entry in entries)
    apparent = _apparent_total(counted)
    allocated = sum(entry["allocated_bytes"] for entry in counted)
    return {
        "role": role,
        "kind": kind,
        "path": str(path),
        "resolved_from": resolved_from,
        "measured": measured,
        "reason": reason,
        "apparent_bytes": apparent,
        "allocated_bytes": allocated,
        "allocated_minus_apparent_bytes": allocated - apparent if measured else None,
        "walk_root": _walk_root_block(path, kind, measured),
        "n_entries": len(entries),
        "n_files": kinds["file"],
        "n_dirs": kinds["dir"],
        "n_symlinks": kinds["symlink"],
        "n_other": kinds["other"],
        "n_hardlink_duplicates": len(entries) - len(counted),
        "n_without_sha256": sum(1 for entry in entries
                                if entry["kind"] == "file" and entry["sha256"] is None),
        "files": entries,
        "errors": errors,
    }


def _measure_directory(role: str, path: Path, resolved_from: str) -> dict:
    """One directory of the install set, walked whole.

    An absent directory is recorded as absent with its reason rather than as a
    zero: an app whose cache directory has never been created and an app whose
    cache directory was emptied report the same total and mean different things.
    """
    if not os.path.lexists(path):
        return _location_block(role, path, resolved_from, "directory", [], [],
                               measured=False, reason=f"{path} does not exist")
    entries, errors = _walk(path)
    _mark_hardlinks(entries)
    return _location_block(role, path, resolved_from, "directory", entries, errors,
                           measured=True, reason=None)


def _measure_file(role: str, path: Path, resolved_from: str) -> dict:
    """One file of the install set, with its symlink target measured as well.

    A link that is itself a role is measured by its own bytes, which would understate the
    install set by an order of magnitude if the target were the app's binary, so the target's
    size and digest travel beside it rather than being summed into the total - the target is
    then counted once, by whichever role walks the directory it lives in.
    """
    if not os.path.lexists(path):
        return _location_block(role, path, resolved_from, "file", [], [],
                               measured=False, reason=f"{path} does not exist")
    st, reason = _lstat(path)
    if st is None:
        return _location_block(role, path, resolved_from, "file", [], [],
                               measured=False, reason=reason)
    entries = [_entry_record(path, path.parent, st)]
    _mark_hardlinks(entries)
    target = _symlink_target(path) if stat.S_ISLNK(st.st_mode) else None
    block = _location_block(role, path, resolved_from, "file", entries, [],
                            measured=True, reason=None)
    block["symlink_target"] = target
    return block


def _resolve_executable(executable: Path, st: os.stat_result) -> Path:
    """Where the executable's bytes are, following the link when there is one.

    An unresolvable link resolves to itself, so the failure shows up as an executable measured
    with no directory around it and a warning, rather than as an invented location.
    """
    if not stat.S_ISLNK(st.st_mode):
        return executable
    try:
        return executable.resolve(strict=True)
    except OSError as exc:
        _warn("disk", "executable link unresolvable, measuring the link itself",
              executable=str(executable), error=str(exc))
        return executable


def _executable_scope(executable: Path, measured_dir: Path) -> dict:
    """What the executable role covered, and what its lexical parent contributed.

    `--executable /usr/bin/goat` names a symlink in a directory every package on the host
    shares. Walking that parent would report /usr/bin as the app's footprint, so the directory
    measured is the one the executable resolves into and the parent is recorded as not walked.
    The warning fires in both directions of that: a parent that was walked has charged its
    other binaries to this app, and a parent that was not has left bytes out that are named.
    """
    parent = executable.parent
    walked = parent == measured_dir
    system = parent.as_posix() in SYSTEM_BINARY_DIRS
    if system:
        _warn("disk", "executable parent is a shared system binary directory",
              executable=str(executable), parent=str(parent), measured_dir=str(measured_dir),
              parent_walked=walked,
              effect="every other binary in it is inside this app's total" if walked
              else "it was not walked; only the resolved directory is inside the total")
    return {"executable": str(executable), "measured_dir": str(measured_dir),
            "lexical_parent": str(parent), "lexical_parent_walked": walked,
            "lexical_parent_is_system_binary_dir": system}


def _measure_executable_dir(role: str, executable: Path, resolved_from: str) -> dict:
    """The installed executable, and the directory it resolves into rather than the one holding it.

    The two are usually the same, and where they are not the difference is the whole point: a
    distribution install puts a link in /usr/bin and the binary in the bundle, and the install
    set is the bundle. The block therefore carries the executable's own record, the directory it
    resolves into walked whole, that directory's root inode under `walk_root`, and the target's
    size and digest under `symlink_target` - so the figure is the binary's, and a reader can see
    which bytes were charged to which.
    """
    if not os.path.lexists(executable):
        return _location_block(role, executable.parent, resolved_from, "directory", [], [],
                               measured=False, reason=f"{executable} does not exist")
    st, reason = _lstat(executable)
    if st is None:
        return _location_block(role, executable.parent, resolved_from, "directory", [], [],
                               measured=False, reason=reason)
    measured_dir = _resolve_executable(executable, st).parent
    entries, errors = _walk(measured_dir)
    entries.insert(0, _entry_record(executable, measured_dir, st))
    _dedupe_paths(entries)
    _mark_hardlinks(entries)
    block = _location_block(role, measured_dir, resolved_from, "directory", entries, errors,
                            measured=True, reason=None)
    block["executable"] = _entry_record(executable, measured_dir, st)
    block["symlink_target"] = _symlink_target(executable)
    block["scope"] = _executable_scope(executable, measured_dir)
    return block


def _symlink_target(path: Path) -> dict | None:
    """The bytes and digest of what a symlink points at, or why there is none."""
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        return {"path": None, "measured": False, "reason": f"link target unresolvable: {exc}",
                "apparent_bytes": None, "allocated_bytes": None, "sha256": None}
    st, reason = _lstat(target)
    if st is None:
        return {"path": str(target), "measured": False, "reason": reason,
                "apparent_bytes": None, "allocated_bytes": None, "sha256": None}
    digest, digest_reason = _sha256(target) if _kind(st) == "file" else (None, "")
    return {"path": str(target), "measured": True,
            "reason": None if _kind(st) == "file" else f"target is a {_kind(st)}, not a file",
            "apparent_bytes": st.st_size,
            "allocated_bytes": st.st_blocks * BLOCK_BYTES,
            "sha256": digest,
            "digest_reason": digest_reason or None}


def _xdg_dir(variable: str, fallback: tuple[str, ...]) -> Path:
    """An XDG base directory, resolved the way Tauri resolves one on Linux.

    The environment variable wins and the default under `$HOME` is the fallback.
    A host with neither is asked to pass the path explicitly rather than have a
    guess written into the record as though it were the app's own resolution.
    """
    value = os.environ.get(variable)
    if value:
        return Path(value)
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ValueError(f"neither {variable} nor HOME is set, so the default path cannot be "
                         f"derived ({exc}); pass the path as an argument instead") from exc
    return home.joinpath(*fallback)


def _dpkg_field(deb: Path, field: str) -> tuple[str | None, str]:
    """One control field of a .deb file, or the reason there is none."""
    try:
        proc = subprocess.run(["dpkg-deb", "--field", str(deb), field],
                              capture_output=True, text=True, timeout=DPKG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"dpkg-deb could not be run: {exc}"
    if proc.returncode != 0:
        return None, f"dpkg-deb exited {proc.returncode}: {proc.stderr.strip()}"
    return proc.stdout.strip(), ""


def _dpkg_block(artefact: Path) -> dict:
    """Installed-Size from the .deb's own control block, or why there is none.

    Reached only for a .deb: Installed-Size is a field of a Debian package's
    control block, so an AppImage or a tarball has none to read. It is in KiB and
    describes the uncompressed installed tree, which is a different quantity from
    the .deb file's own size - the two sit side by side here rather than one
    standing in for the other.
    """
    if artefact.suffix.lower() != ".deb":
        return {"applicable": False, "installed_size_bytes": None, "package": None,
                "version": None, "architecture": None, "reason":
                "the artefact is not a .deb, and Installed-Size is a field of a Debian "
                "package's control block, so there is nothing to read",
                "method": None}
    fields = {name: _dpkg_field(artefact, name) for name in
              ("Package", "Version", "Architecture", "Installed-Size")}
    raw, reason = fields["Installed-Size"]
    if raw is None:
        installed, reason_bytes = None, reason
    elif raw.isdigit():
        installed, reason_bytes = int(raw) * 1024, ""
    else:
        installed, reason_bytes = None, f"Installed-Size is not an integer: {raw!r}"
    return {
        "applicable": True,
        "installed_size_bytes": installed,
        "package": fields["Package"][0],
        "version": fields["Version"][0],
        "architecture": fields["Architecture"][0],
        "reason": reason_bytes or None,
        "method": "dpkg-deb --field <artefact> Installed-Size, in KiB, multiplied by 1024; "
                  "it is the uncompressed installed size and is not the .deb file's own size",
    }


def _install_set(locations: dict[str, dict]) -> dict:
    """One total per figure over the union of the enumerated locations.

    The roles are enumerated separately because that is what makes the enumeration checkable, and
    they are not guaranteed disjoint - a sidecar binary normally sits inside the executable's
    directory. The union counts each absolute path once and names every path a second role
    re-reported, so a reader can tell `sum_over_roles` from the union instead of finding two
    totals that differ with nothing to say why.
    """
    owner: dict[str, str] = {}
    repeated: list[dict] = []
    owned: list[dict] = []
    for role, block in locations.items():
        for entry in block["files"]:
            if not entry.get("counted_in_totals", True):
                continue
            already = owner.get(entry["abs_path"])
            if already is not None:
                repeated.append({"abs_path": entry["abs_path"], "counted_under": already,
                                 "repeated_by": role})
                continue
            owner[entry["abs_path"]] = role
            owned.append(entry)
    apparent = _apparent_total(owned)
    allocated = sum(entry["allocated_bytes"] for entry in owned)
    measured = [block for block in locations.values() if block["measured"]]
    return {
        "apparent_bytes": apparent,
        "allocated_bytes": allocated,
        "allocated_minus_apparent_bytes": allocated - apparent,
        "sum_over_roles_apparent_bytes": sum(block["apparent_bytes"] for block in measured),
        "sum_over_roles_allocated_bytes": sum(block["allocated_bytes"] for block in measured),
        "n_distinct_paths": len(owner),
        "n_roles": len(locations),
        "n_roles_measured": len(measured),
        "roles": sorted(locations),
        "paths_reported_by_more_than_one_role": repeated,
        "note": "sum_over_roles_* adds every role and so double-counts any path listed under "
                "paths_reported_by_more_than_one_role; the union figures above count each "
                "absolute path once. Both are given because neither alone is checkable.",
    }


def _budget_block(budget_bytes: int | None, install_set: dict) -> dict:
    """The declared storage budget compared against the measured figures.

    The budget has no default: the script states the comparison only for a budget
    the operator passes, because a threshold the script chose itself would be a
    threshold no document in this repository says.
    """
    if budget_bytes is None:
        return {"declared_storage_budget_bytes": None, "within_budget": None,
                "apparent_headroom_bytes": None, "allocated_headroom_bytes": None,
                "reason": "no storage budget was passed, so this run makes no comparison",
                "note": "A comparison here is a claim about a threshold, and the threshold is "
                        "not a measurement; pass the declared budget to make one."}
    return {
        "declared_storage_budget_bytes": budget_bytes,
        "within_budget": {
            "apparent": install_set["apparent_bytes"] <= budget_bytes,
            "allocated": install_set["allocated_bytes"] <= budget_bytes,
        },
        "apparent_headroom_bytes": budget_bytes - install_set["apparent_bytes"],
        "allocated_headroom_bytes": budget_bytes - install_set["allocated_bytes"],
        "reason": None,
        "note": "Both figures are compared. A budget written as 'N GB' is ambiguous between "
                "1e9 and 2**30 bytes, so the budget is passed here as an exact byte count "
                "and the unit it was converted from belongs in the artefact.",
    }


def _host_block() -> dict:
    """Host facts a footprint figure is only valid for."""
    meminfo = Path("/proc/meminfo")
    mem_total = None
    try:
        first = meminfo.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        mem_total = int(first.split()[1]) * 1024
    except (OSError, IndexError, ValueError) as exc:
        mem_total = None
        _warn("disk", "host memory total unreadable", error=str(exc))
    uname = platform.uname()
    return {"os": uname.system, "kernel": uname.release, "cpu_model": uname.machine,
            "cpu_cores_total": os.cpu_count(), "memory_total_bytes": mem_total,
            "python_version": platform.python_version()}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Disk footprint of an installed GOaT app.")
    parser.add_argument("--artefact", type=Path, required=True,
                        help="the install artefact as distributed - AppImage, .deb or tarball")
    parser.add_argument("--resource-dir", type=Path, required=True,
                        help="the directory Tauri's resource_dir() resolves to on this install")
    parser.add_argument("--executable", type=Path, required=True,
                        help="the installed main executable; it and the directory it resolves "
                             "into are measured, so a link in a shared system directory does "
                             "not bring that whole directory into the figure")
    parser.add_argument("--app-data-dir", type=Path,
                        help=f"app_data_dir(); default $XDG_DATA_HOME/{APP_IDENTIFIER}")
    parser.add_argument("--app-cache-dir", type=Path,
                        help=f"app_cache_dir(); default $XDG_CACHE_HOME/{APP_IDENTIFIER}")
    parser.add_argument("--sidecar", type=Path,
                        help="the OCR sidecar binary; absent on an install that ships none")
    parser.add_argument("--desktop-file", type=Path,
                        help=f"the desktop entry; default $XDG_DATA_HOME/applications/"
                             f"{APP_IDENTIFIER}.desktop")
    parser.add_argument("--storage-budget-bytes", type=int,
                        help="declared storage budget to compare the total against; "
                             "no comparison is made when this is absent")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _roles(args: argparse.Namespace) -> list[tuple[str, str, Path, str]]:
    """The enumerated install set: role, kind, path, and where that path came from.

    Every role records its origin because a path this script derived from an XDG variable and a
    path the operator read off the running app are different claims even when the strings match.
    A role the operator did not pass and that has no XDG rule - the sidecar - is left out rather
    than guessed at, and its absence from this list is the only record that it was not measured.
    """
    data_home = _xdg_dir("XDG_DATA_HOME", (".local", "share"))
    roles: list[tuple[str, str, Path, str]] = [
        ("artefact", "file", args.artefact, "argument --artefact"),
        ("executable_dir", "executable", args.executable,
         "argument --executable, together with the directory it resolves into"),
        ("resource_dir", "directory", args.resource_dir, "argument --resource_dir"),
    ]
    for role, flag, override, default in (
        ("app_data_dir", "--app-data-dir", args.app_data_dir, data_home / APP_IDENTIFIER),
        ("app_cache_dir", "--app-cache-dir", args.app_cache_dir,
         _xdg_dir("XDG_CACHE_HOME", (".cache",)) / APP_IDENTIFIER),
    ):
        chosen = override or default
        roles.append((role, "directory", chosen,
                      f"argument {flag}" if override else f"default derived from {chosen.parent}"))
    if args.sidecar is not None:
        roles.append(("sidecar", "file", args.sidecar, "argument --sidecar"))
    desktop = args.desktop_file or data_home / "applications" / f"{APP_IDENTIFIER}.desktop"
    roles.append(("desktop_file", "file", desktop, "argument --desktop-file" if args.desktop_file
                  else f"default derived from {desktop.parent}"))
    return roles


def _report_roles(locations: dict[str, dict]) -> None:
    """Fail on a missing required role; warn about every other absence and walk error.

    A role that is absent stays in the artefact with its reason, so an install with no sidecar
    and an install whose sidecar was not passed are distinguishable after the run. Nothing is
    dropped and nothing is written as zero.
    """
    missing = [role for role in REQUIRED_ROLES if not locations[role]["measured"]]
    if missing:
        raise RuntimeError("the install set cannot be reported without these locations: "
                           + ", ".join(f"{role} ({locations[role]['reason']})" for role in missing))
    for role, block in locations.items():
        if not block["measured"]:
            _warn("disk", "location not present", role=role, path=block["path"],
                  reason=block["reason"])
        for error in block["errors"]:
            _warn("disk", "walk error", role=role, **error)


def _summary(args: argparse.Namespace, locations: dict[str, dict],
             install_set: dict) -> dict:
    """The artefact: the enumerated install set, the union total, and the three figures."""
    return {
        "schema": SCHEMA,
        "produced_by": "GOaT/model/scripts/measure_disk.py",
        "measured_at_unix": int(time.time()),
        "unit": "bytes everywhere; st_blocks is counted in 512-byte units on every filesystem, "
                "and apparent_bytes excludes directory inodes as du --apparent-size does",
        "method": {
            "apparent_bytes": "sum of st_size over every non-directory entry the walk returned, "
                              "each inode counted once - what du --apparent-size reports and "
                              "what a file manager shows. A directory's st_size is the size of "
                              "the directory entry, not of its contents, and du excludes it",
            "allocated_bytes": "sum of st_blocks * 512 over the same entries, directory inodes "
                               "included - what du reports and what the filesystem reserved",
            "dpkg_installed_size_bytes": "dpkg-deb --field Installed-Size x 1024, read only "
                                         "when the artefact is a .deb",
            "sha256": "content digest of every regular file, read in 4 MiB chunks",
            "walk_root": "os.walk names the root but yields its children, so a walked "
                         "directory's own inode is not in files[]; its st_size and blocks are "
                         "recorded per role under walk_root. du counts the starting directory's "
                         "blocks in its allocated total, so for a role whose entries all sit "
                         "under the walk root a du figure equals this allocated total plus that "
                         "role's walk_root allocated_bytes. executable_dir is not such a role: "
                         "it also lists the executable itself, and a link outside the walked "
                         "directory carries its own st_size into this apparent total while no "
                         "du figure over the walked path contains it; on a filesystem that "
                         "gives a symlink blocks of its own the same holds for allocated_bytes, "
                         "on one that stores a fast link inside its inode it does not",
            "symlink_policy": "lstat only, never followed; a symlink contributes its own few "
                              "bytes, and the installed executable's target is measured beside "
                              "it under symlink_target rather than summed in, because the "
                              "directory that resolves the link already contains those bytes",
            "hardlink_policy": "counted once per inode; later entries stay in the file list "
                               "marked counted_in_totals false, pointing at the entry carrying "
                               "them",
            "walk_order": "os.walk with sorted names, no symlinked directory followed",
            "executable_scope": "the executable itself plus the directory it resolves into, "
                                "never the lexical parent that merely holds a link to it; "
                                "scope records the lexical parent, whether it was walked, and "
                                "whether it is a shared system binary directory",
        },
        "host": _host_block(),
        "locations": locations,
        "install_set": install_set,
        "dpkg": _dpkg_block(args.artefact),
        "budget": _budget_block(args.storage_budget_bytes, install_set),
    }


@log_call
def main() -> None:
    args = _parse_args()
    measurers = {"file": _measure_file, "executable": _measure_executable_dir,
                 "directory": _measure_directory}
    locations = {role: measurers[kind](role, path, resolved_from)
                 for role, kind, path, resolved_from in _roles(args)}
    _report_roles(locations)
    install_set = _install_set(locations)
    summary = _summary(args, locations, install_set)
    write_json(args.output, summary)
    _info("disk", "wrote", out=str(args.output), roles=install_set["roles"],
          apparent_bytes=install_set["apparent_bytes"],
          allocated_bytes=install_set["allocated_bytes"],
          n_files=sum(block["n_files"] for block in locations.values()))


if __name__ == "__main__":
    main()
