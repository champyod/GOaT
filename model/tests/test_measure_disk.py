"""Disk footprint figures, pinned to what the artefact says about them.

No install is measured here: every tree is built under `tmp_path`, so the whole
file runs in milliseconds with no dpkg call and no artefact. What it pins are the
decisions a footprint figure rests on and a reader cannot re-derive from a total —
a directory inode is left out of the apparent figure and kept in the allocated one,
a hard-linked file is counted once per inode while staying in the file list, an
absent role and an empty one report different things despite the same zeros, and a
link pointing into another directory is scoped to the directory it resolves into
with its lexical parent named rather than walked.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_MODEL = _HERE.parents[1]
for _entry in (str(_MODEL / "src"), str(_MODEL / "scripts")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import measure_disk as disk  # the sys.path entries above have to come first

FROM_RESOURCE_DIR = "argument --resource-dir"
BINARY = b"GOATBIN"
SIDECAR = b"SIDEC"
ICON = b"PNG"
SYSTEM_PARENT_WARNING = "executable parent is a shared system binary directory"
ABSENT = "does not exist"

#: What `_warn` was told, as `(tag, message, fields)` in the order the calls came.
Warning = tuple[str, str, dict[str, object]]


@pytest.fixture()
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[Warning]:
    """Every warning the script made, so a run that is expected to be quiet can be held to it."""
    seen: list[Warning] = []

    def record(tag: str, message: str, **fields: object) -> None:
        seen.append((tag, message, fields))

    monkeypatch.setattr(disk, "_warn", record)
    return seen


def _tree(root: Path) -> Path:
    """A resource directory holding one file and a subdirectory holding one file."""
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "a.bin").write_bytes(BINARY)
    (sub / "b.bin").write_bytes(SIDECAR)
    return root


def _counted(block: dict) -> list[dict]:
    """The entries a block's totals are drawn from, as the file list marks them."""
    return [entry for entry in block["files"] if entry["counted_in_totals"]]


def _apparent(entries: list[dict]) -> int:
    """The apparent figure re-derived from the file list printed beside it."""
    return sum(entry["apparent_bytes"] for entry in entries if entry["kind"] != "dir")


def test_a_directory_inode_leaves_the_apparent_figure_and_stays_in_the_allocated_one() -> None:
    """The two figures part on directories, and that is the only thing they part on.

    du takes a directory's own `st_size` out of its apparent total and counts the
    directory's blocks in its allocated one, so a figure a reader can compare with
    du has to make those same two decisions. The sizes below are the script's own
    vocabulary, so the predicate is pinned rather than the host filesystem's idea of
    a block, which would otherwise decide whether this says anything at all.
    """
    entries = [
        {"kind": "file", "apparent_bytes": 3, "allocated_bytes": disk.BLOCK_BYTES * 8,
         "sha256": "digest"},
        {"kind": "dir", "apparent_bytes": 4096, "allocated_bytes": disk.BLOCK_BYTES * 16,
         "sha256": None},
        {"kind": "file", "apparent_bytes": 2, "allocated_bytes": disk.BLOCK_BYTES * 8,
         "sha256": "digest"},
        {"kind": "symlink", "apparent_bytes": 17, "allocated_bytes": 0, "sha256": None},
    ]

    block = disk._location_block("resource_dir", Path("/fake/resources"), FROM_RESOURCE_DIR,
                                 "directory", entries, [], measured=True, reason=None)

    assert disk._apparent_total(entries) == 3 + 2 + 17
    assert block["apparent_bytes"] == 3 + 2 + 17
    assert block["allocated_bytes"] == disk.BLOCK_BYTES * 32
    assert block["allocated_minus_apparent_bytes"] == disk.BLOCK_BYTES * 32 - (3 + 2 + 17)
    assert [block["n_files"], block["n_dirs"], block["n_symlinks"]] == [2, 1, 1]
    assert block["n_other"] == 0


def test_a_walked_directory_reports_a_du_comparable_apparent_total(tmp_path: Path) -> None:
    root = _tree(tmp_path / "resources")

    block = disk._measure_directory("resource_dir", root, FROM_RESOURCE_DIR)

    assert block["measured"] is True and block["reason"] is None
    assert block["apparent_bytes"] == len(BINARY) + len(SIDECAR)
    assert block["n_files"] == 2 and block["n_dirs"] == 1
    assert block["n_hardlink_duplicates"] == 0 and block["n_without_sha256"] == 0
    assert block["errors"] == [] and _apparent(_counted(block)) == block["apparent_bytes"]

    # Every directory the walk returned carries a real st_size, so leaving it out was a
    # decision rather than a coincidence: adding them back is what the figure is not.
    directories = [entry for entry in block["files"] if entry["kind"] == "dir"]
    assert directories and all(entry["apparent_bytes"] > 0 for entry in directories)
    assert (block["apparent_bytes"] + sum(entry["apparent_bytes"] for entry in directories)
            == sum(entry["apparent_bytes"] for entry in block["files"]))
    assert block["allocated_bytes"] == sum(entry["allocated_bytes"] for entry in block["files"])

    # `os.walk` names the directory it starts from but yields its children, so that
    # inode is held aside under walk_root: du counts it and the apparent figure above
    # does not, and the reader is the one who gets to see both.
    inode = root.lstat()
    assert inode.st_size > 0
    assert all(entry["abs_path"] != str(root) for entry in block["files"])
    assert block["walk_root"] == {"path": str(root), "kind": "dir",
                                  "apparent_bytes": inode.st_size,
                                  "allocated_bytes": inode.st_blocks * disk.BLOCK_BYTES,
                                  "reason": None}


def test_a_hard_linked_file_is_counted_once_and_the_duplicate_stays_listed(tmp_path: Path) -> None:
    root = _tree(tmp_path / "resources")
    os.link(root / "a.bin", root / "c.bin")

    block = disk._measure_directory("resource_dir", root, FROM_RESOURCE_DIR)

    listed = {entry["path"]: entry for entry in block["files"]}
    assert set(listed) == {"a.bin", "c.bin", "sub", "sub/b.bin"}
    assert [listed["a.bin"]["nlink"], listed["c.bin"]["nlink"]] == [2, 2]
    assert listed["a.bin"]["counted_in_totals"] is True and listed["a.bin"]["duplicate_of"] is None
    assert listed["c.bin"]["counted_in_totals"] is False
    assert listed["c.bin"]["duplicate_of"] == str(root / "a.bin")

    # Two names for one inode is one figure, and the list beside it still says which
    # entry is carrying them and what the other one holds.
    assert block["n_hardlink_duplicates"] == 1
    assert block["apparent_bytes"] == len(BINARY) + len(SIDECAR)
    every_name = sum(entry["apparent_bytes"] for entry in block["files"]
                     if entry["kind"] == "file")
    assert every_name == block["apparent_bytes"] + len(BINARY)
    assert _apparent(_counted(block)) == block["apparent_bytes"]
    assert listed["c.bin"]["sha256"] == listed["a.bin"]["sha256"]
    assert listed["c.bin"]["sha256"] == hashlib.sha256(BINARY).hexdigest()


def test_the_union_counts_a_path_once_where_the_roles_added_together_count_it_again(
        tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    icons = bundle / "icons"
    icons.mkdir(parents=True)
    (bundle / "goat").write_bytes(BINARY)
    (icons / "goat.png").write_bytes(ICON)
    os.link(icons / "goat.png", icons / "goat@2x.png")

    executable = disk._measure_executable_dir("executable_dir", bundle / "goat",
                                              "argument --executable")
    resources = disk._measure_directory("resource_dir", bundle, FROM_RESOURCE_DIR)
    union = disk._install_set({"executable_dir": executable, "resource_dir": resources})

    listed = len(BINARY) + len(ICON)
    assert resources["apparent_bytes"] == executable["apparent_bytes"] == listed
    assert union["apparent_bytes"] == listed
    assert union["n_distinct_paths"] == 3
    assert union["n_roles"] == union["n_roles_measured"] == 2
    assert union["allocated_minus_apparent_bytes"] == union["allocated_bytes"] - listed

    # Two roles are not guaranteed disjoint, so the roles added together double the
    # overlap and the union does not. Both totals are given, and the difference is
    # named rather than left for a reader to infer from two numbers that disagree.
    assert union["sum_over_roles_apparent_bytes"] == 2 * listed
    repeated = union["paths_reported_by_more_than_one_role"]
    assert {entry["abs_path"] for entry in repeated} == {
        str(bundle / "goat"), str(icons), str(icons / "goat.png")}
    assert all(entry["counted_under"] == "executable_dir"
               and entry["repeated_by"] == "resource_dir" for entry in repeated)
    # The hard link is inside that: one inode for two names inside a role, and one name
    # for two roles. Neither pass lets the pair back in, so neither figure doubles it.
    assert str(icons / "goat@2x.png") not in {entry["abs_path"] for entry in repeated}


def test_an_absent_role_and_an_empty_one_report_different_things(tmp_path: Path) -> None:
    never_created = tmp_path / "never-created"
    emptied = tmp_path / "emptied"
    emptied.mkdir()
    from_flag = "argument --app-cache-dir"

    gone = disk._measure_directory("app_cache_dir", never_created, from_flag)
    blank = disk._measure_directory("app_cache_dir", emptied, from_flag)

    # Every counter is the same zero in both, so what tells a cache directory that was
    # never created from one that was emptied is carried entirely by these three.
    assert (gone["apparent_bytes"], gone["allocated_bytes"], gone["n_entries"], gone["n_files"],
            gone["n_dirs"]) == (blank["apparent_bytes"], blank["allocated_bytes"],
                                blank["n_entries"], blank["n_files"], blank["n_dirs"]) == \
        (0, 0, 0, 0, 0)
    assert gone["files"] == blank["files"] == [] and gone["errors"] == blank["errors"] == []

    assert gone["measured"] is False and gone["reason"] == f"{never_created} {ABSENT}"
    assert gone["walk_root"] is None and gone["allocated_minus_apparent_bytes"] is None
    assert blank["measured"] is True and blank["reason"] is None
    assert blank["allocated_minus_apparent_bytes"] == 0
    assert blank["walk_root"]["path"] == str(emptied)
    assert blank["walk_root"]["apparent_bytes"] == emptied.lstat().st_size


def test_a_missing_required_role_refuses_the_run_where_a_missing_optional_one_does_not(
        tmp_path: Path, warnings: list[Warning]) -> None:
    absent = tmp_path / "absent"
    missing = {role: disk._measure_directory(role, absent / role, f"argument {role}")
               for role in disk.REQUIRED_ROLES}

    with pytest.raises(RuntimeError) as raised:
        disk._report_roles(missing)

    message = str(raised.value)
    assert "cannot be reported without these locations" in message
    assert all(role in message for role in disk.REQUIRED_ROLES)
    assert ABSENT in message
    assert warnings == []  # a refused run says what is missing rather than listing it

    complete = {role: disk._measure_directory(role, _tree(tmp_path / role), f"argument {role}")
                for role in disk.REQUIRED_ROLES}
    complete["sidecar"] = disk._measure_file("sidecar", absent / "sidecar", "argument --sidecar")
    complete["desktop_file"] = disk._measure_file("desktop_file", absent / "goat.desktop",
                                                  "argument --desktop-file")

    assert disk._report_roles(complete) is None
    # An install that ships no sidecar is a fact about that install, so it is warned
    # about and kept in the artefact rather than dropped, refused or written as a zero.
    assert [(tag, said) for tag, said, _ in warnings] == [("disk", "location not present")] * 2
    assert [fields["role"] for _, _, fields in warnings] == ["sidecar", "desktop_file"]
    assert complete["sidecar"]["reason"] == f"{absent / 'sidecar'} {ABSENT}"
    assert complete["sidecar"]["measured"] is False


def test_a_link_is_scoped_to_the_directory_it_resolves_into_and_never_to_its_parent(
        tmp_path: Path, warnings: list[Warning]) -> None:
    bundle = tmp_path / "bundle"
    binary_dir = tmp_path / "usr" / "bin"
    bundle.mkdir()
    binary_dir.mkdir(parents=True)
    (bundle / "goat").write_bytes(BINARY)
    (bundle / "sidecar").write_bytes(SIDECAR)
    link = binary_dir / "goat"
    os.symlink(bundle / "goat", link)

    block = disk._measure_executable_dir("executable_dir", link, "argument --executable")

    assert block["measured"] is True and block["path"] == str(bundle)
    assert block["scope"] == {"executable": str(link), "measured_dir": str(bundle),
                              "lexical_parent": str(binary_dir), "lexical_parent_walked": False,
                              "lexical_parent_is_system_binary_dir": False}
    assert warnings == []  # a parent outside the shared system directories needs no warning

    # The link brings its own bytes and its target's travel beside the figure rather
    # than being summed in: the directory that resolves the link already holds them,
    # so adding the target here would charge the binary to the app twice.
    link_bytes = os.lstat(link).st_size
    assert block["symlink_target"]["measured"] is True
    assert block["symlink_target"]["apparent_bytes"] == len(BINARY)
    assert block["symlink_target"]["sha256"] == hashlib.sha256(BINARY).hexdigest()
    assert block["apparent_bytes"] == link_bytes + len(BINARY) + len(SIDECAR)
    assert block["executable"]["kind"] == "symlink" and block["executable"]["sha256"] is None

    # The executable is listed explicitly and again by the walk beside it, so it is
    # listed once, and the target it resolves into is walked as a directory of its own.
    paths = [entry["abs_path"] for entry in block["files"]]
    assert paths.count(str(bundle / "goat")) == 1
    assert paths.count(str(link)) == 1
    assert len(paths) == len(set(paths)) == 3


def test_a_shared_system_binary_parent_is_named_and_warned_about_in_both_directions(
        tmp_path: Path, warnings: list[Warning]) -> None:
    local_bin = tmp_path / "usr" / "bin"

    held_out = disk._executable_scope(Path("/usr/bin/goat"), tmp_path / "bundle")
    charged = disk._executable_scope(Path("/usr/bin/goat"), Path("/usr/bin"))
    quiet = disk._executable_scope(local_bin / "goat", local_bin)

    # A distribution install puts the link in a directory every package on the host
    # shares, so the two cases say opposite things and neither is the quiet one.
    assert (held_out["lexical_parent"], held_out["lexical_parent_is_system_binary_dir"],
            held_out["lexical_parent_walked"]) == ("/usr/bin", True, False)
    assert (charged["lexical_parent"], charged["lexical_parent_is_system_binary_dir"],
            charged["lexical_parent_walked"]) == ("/usr/bin", True, True)
    assert (quiet["lexical_parent"], quiet["lexical_parent_is_system_binary_dir"],
            quiet["lexical_parent_walked"]) == (str(local_bin), False, True)

    assert [said for _, said, _ in warnings] == [SYSTEM_PARENT_WARNING] * 2
    assert [fields["parent"] for _, _, fields in warnings] == ["/usr/bin", "/usr/bin"]
    assert [fields["parent_walked"] for _, _, fields in warnings] == [False, True]
    assert [fields["effect"] for _, _, fields in warnings] == [
        "it was not walked; only the resolved directory is inside the total",
        "every other binary in it is inside this app's total",
    ]