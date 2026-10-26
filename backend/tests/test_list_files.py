from __future__ import annotations

import os
import subprocess

import pytest

from xodus.capabilities.list_files import ListFiles
from xodus.capabilities.registry import InvalidInput, InvocationContext, NotFound, PermissionDenied


def run(cap: ListFiles, path: str) -> dict:
    return cap.run({"path": path}, InvocationContext(task_id=None, step_index=0))


def test_lists_root_and_subfolder(sandbox):
    cap = ListFiles("sandbox", sandbox)
    root = run(cap, ".")
    assert root["path"] == "."
    assert [(e["name"], e["type"]) for e in root["entries"]] == [("sub", "dir"), ("a.txt", "file")]
    assert root["entries"][1]["size"] == 5
    assert [e["name"] for e in run(cap, "sub")["entries"]] == ["b.txt"]


ESCAPES = [
    "..", "../outside", "sub/../../outside", "C:\\Windows", "C:relative", "\\Windows", "/etc",
    "\\\\server\\share", "//server/share", "\\\\?\\C:\\Windows", "\\\\.\\pipe\\x", "a.txt:secret", "sub:$DATA",
    "CON", "nul.txt", "com1", "COM¹", "PROGRA~1", "bad\x00name", "sub.", "sub ", "su*b", "s?b",
]


@pytest.mark.parametrize("path", ESCAPES)
def test_escape_attempts_are_denied(sandbox, path):
    with pytest.raises(PermissionDenied):
        run(ListFiles("sandbox", sandbox), path)


def test_junction_is_not_followed(sandbox, tmp_path):
    link = sandbox / "jump"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "outside")],
                            capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip(f"could not create junction: {result.stdout} {result.stderr}")
    cap = ListFiles("sandbox", sandbox)
    with pytest.raises(PermissionDenied, match="junction"):
        run(cap, "jump")
    listing = {e["name"]: e["type"] for e in run(cap, ".")["entries"]}
    assert listing["jump"] == "link"  # shown, never followed
    assert "secret.txt" not in str(run(cap, "."))


def test_symlink_is_not_followed(sandbox, tmp_path):
    try:
        os.symlink(tmp_path / "outside", sandbox / "sym", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks need Developer Mode or elevation here: {exc}")
    with pytest.raises(PermissionDenied):
        run(ListFiles("sandbox", sandbox), "sym")


def test_missing_folder_and_file_path(sandbox):
    cap = ListFiles("sandbox", sandbox)
    with pytest.raises(NotFound):
        run(cap, "nope")
    with pytest.raises(InvalidInput):
        run(cap, "a.txt")


def test_entry_cap_sets_truncated(sandbox):
    for i in range(5):
        (sandbox / f"f{i}.txt").write_text("x")
    out = run(ListFiles("sandbox", sandbox, max_entries=3), ".")
    assert len(out["entries"]) == 3 and out["truncated"] is True


def test_manifest_declares_path_argument(sandbox):
    manifest = ListFiles("sandbox", sandbox).manifest
    assert manifest.path_arguments() == {"path": ("fs.read:sandbox", "dir")}
    assert manifest.input_schema["additionalProperties"] is False
    assert manifest.side_effects == "none" and manifest.requires_approval == "never"
