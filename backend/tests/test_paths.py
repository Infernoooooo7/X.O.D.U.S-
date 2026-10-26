"""Path confinement and atomic writes (security table: path traversal; approval row: no partial file)."""

from __future__ import annotations

import subprocess

import pytest

from xodus.security.paths import (
    PathNotFound,
    PathViolation,
    WriteCancelled,
    WrongKind,
    atomic_write_bytes,
    resolve_existing,
    resolve_new_file,
    split_relative,
)


@pytest.mark.parametrize("rel", [".", "", "sub", "sub/b.txt", "./sub", "sub\\b.txt", ".hidden", "notes v2", "über"])
def test_benign_relative_paths_are_accepted(rel):
    split_relative(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "..", "a/../..", "C:\\x", "c:x", "\\x", "/x", "\\\\srv\\share", "//srv/share", "\\\\?\\C:\\x", "\\\\.\\COM1",
        "\\\\?\\UNC\\srv\\share", "f.txt:stream", "f.txt::$DATA", "CON", "con.txt", "Nul", "AUX.log", "COM3", "LPT1.txt",
        "COM²", "CONIN$", "PROGRA~1", "dir\\LONGNA~2\\x", "trail.", "trail ", "a\\b.\\c", "wild*", "q?", "pipe|x",
        "lt<", 'quote"', "ctl\x01", "nul\x00byte", "x" * 300, 5, None, ["a"],
    ],
)
def test_hostile_paths_are_rejected(rel):
    with pytest.raises(PathViolation):
        split_relative(rel)


def test_resolve_existing_kinds(sandbox):
    assert resolve_existing(sandbox, "sub", "dir") == (sandbox / "sub").resolve()
    assert resolve_existing(sandbox, "a.txt", "file") == (sandbox / "a.txt").resolve()
    with pytest.raises(WrongKind):
        resolve_existing(sandbox, "a.txt", "dir")
    with pytest.raises(WrongKind):
        resolve_existing(sandbox, "sub", "file")
    with pytest.raises(PathNotFound):
        resolve_existing(sandbox, "missing", "dir")


def test_junction_inside_path_is_refused_for_reads_and_writes(sandbox, tmp_path):
    link = sandbox / "jump"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "outside")], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(PathViolation, match="junction"):
        resolve_existing(sandbox, "jump/secret.txt", "file")
    with pytest.raises(PathViolation, match="junction"):
        resolve_new_file(sandbox, "jump/new.txt")
    assert not (tmp_path / "outside" / "new.txt").exists()


def test_root_that_is_a_junction_is_refused(sandbox, tmp_path):
    link = tmp_path / "rootlink"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(sandbox)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(PathViolation, match="root is itself a link"):
        resolve_existing(link, ".", "dir")


def test_resolve_new_file(sandbox):
    assert resolve_new_file(sandbox, "sub/new.txt") == (sandbox / "sub").resolve() / "new.txt"
    with pytest.raises(PathViolation):
        resolve_new_file(sandbox, ".")
    with pytest.raises(WrongKind):
        resolve_new_file(sandbox, "sub")
    with pytest.raises(PathNotFound):
        resolve_new_file(sandbox, "nofolder/new.txt")
    with pytest.raises(PathViolation):
        resolve_new_file(sandbox, "../outside/new.txt")


def _leftovers(folder):
    return [p.name for p in folder.iterdir() if p.name.endswith(".xodus-tmp")]


def test_atomic_write_replaces_whole_file(sandbox):
    target = sandbox / "a.txt"
    atomic_write_bytes(target, b"new content", chunk_size=3)
    assert target.read_bytes() == b"new content"
    assert _leftovers(sandbox) == []


def test_cancelled_write_leaves_target_untouched_and_no_temp_file(sandbox):
    target = sandbox / "a.txt"
    calls = {"n": 0}

    def cancel_after_two_chunks() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(WriteCancelled):
        atomic_write_bytes(target, b"0123456789" * 10, cancelled=cancel_after_two_chunks, chunk_size=4)
    assert target.read_bytes() == b"hello"  # original content
    assert _leftovers(sandbox) == []


def test_cancelled_new_file_is_never_created(sandbox):
    target = sandbox / "fresh.txt"
    with pytest.raises(WriteCancelled):
        atomic_write_bytes(target, b"abcdef", cancelled=lambda: True, chunk_size=2)
    assert not target.exists() and _leftovers(sandbox) == []


def test_failed_write_cleans_up(sandbox, monkeypatch):
    import os as _os

    def boom(*_a, **_k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_bytes(sandbox / "a.txt", b"zzz")
    monkeypatch.undo()
    assert (sandbox / "a.txt").read_bytes() == b"hello" and _leftovers(sandbox) == []
