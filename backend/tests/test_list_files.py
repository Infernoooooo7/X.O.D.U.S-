from __future__ import annotations

import dataclasses
import os
import subprocess

import pytest

from xodus.capabilities.list_files import ListFiles, ListFilesInput
from xodus.capabilities.registry import (
    CapabilityRegistry,
    InvalidInput,
    NotFound,
    OutputTooLarge,
    PermissionDenied,
    UnknownCapability,
)


def run(cap: ListFiles, path: str) -> dict:
    return cap.run(ListFilesInput(path=path))


def test_lists_root_and_subfolder(sandbox):
    cap = ListFiles(sandbox)
    root = run(cap, ".")
    assert root["path"] == "."
    assert [(e["name"], e["type"]) for e in root["entries"]] == [("sub", "dir"), ("a.txt", "file")]
    assert root["entries"][1]["size"] == 5
    assert [e["name"] for e in run(cap, "sub")["entries"]] == ["b.txt"]


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../outside",
        "sub/../../outside",
        "C:\\Windows",
        "C:relative",
        "\\Windows",
        "/etc",
        "\\\\server\\share",
        "//server/share",
        "\\\\?\\C:\\Windows",
        "a.txt:secret",
        "sub:$DATA",
        "CON",
        "nul.txt",
        "com1",
        "PROGRA~1",
        "bad\x00name",
    ],
)
def test_escape_attempts_are_denied(sandbox, path):
    with pytest.raises(PermissionDenied):
        run(ListFiles(sandbox), path)


def test_junction_is_not_followed(sandbox, tmp_path):
    link = sandbox / "jump"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "outside")], capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.skip(f"could not create junction: {result.stdout} {result.stderr}")
    cap = ListFiles(sandbox)
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
        run(ListFiles(sandbox), "sym")


def test_missing_folder_and_file_path(sandbox):
    cap = ListFiles(sandbox)
    with pytest.raises(NotFound):
        run(cap, "nope")
    with pytest.raises(InvalidInput):
        run(cap, "a.txt")


def test_entry_cap_sets_truncated(sandbox):
    for i in range(5):
        (sandbox / f"f{i}.txt").write_text("x")
    out = run(ListFiles(sandbox, max_entries=3), ".")
    assert len(out["entries"]) == 3 and out["truncated"] is True


async def test_registry_validates_input_and_unknown_ids(sandbox):
    reg = CapabilityRegistry()
    reg.register(ListFiles(sandbox))
    assert (await reg.invoke("fs.list_files", {}))["path"] == "."
    with pytest.raises(InvalidInput):
        await reg.invoke("fs.list_files", {"path": "x", "recursive": True})  # undeclared argument
    with pytest.raises(InvalidInput):
        await reg.invoke("fs.list_files", {"path": 5})
    with pytest.raises(UnknownCapability):
        await reg.invoke("fs.delete_everything", {})
    desc = reg.describe()[0]
    assert desc["side_effects"] == "none" and desc["requires_approval"] == "never"
    assert desc["input_schema"]["properties"]["path"]["type"] == "string"


async def test_registry_enforces_output_cap(sandbox):
    for i in range(50):
        (sandbox / f"long-file-name-{i:03d}.txt").write_text("x")
    cap = ListFiles(sandbox)
    cap.manifest = dataclasses.replace(cap.manifest, max_output_bytes=200)
    reg = CapabilityRegistry()
    reg.register(cap)
    with pytest.raises(OutputTooLarge):
        await reg.invoke("fs.list_files", {})
