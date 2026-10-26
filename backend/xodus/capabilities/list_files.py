"""fs.list_files: list one folder inside a single configured root. Read-only, never follows links.

Path rules (enforced before touching the filesystem, then re-checked on the resolved path):
relative paths only; no drive letters, UNC or rooted paths; no '..'; no ':' (alternate data
streams); no reserved device names (CON, NUL, COM1, ...); no 8.3 short-name components (FOO~1);
no symlink or junction anywhere on the way; the resolved path must stay inside the root.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from xodus.capabilities.registry import CapabilityManifest, InvalidInput, NotFound, PermissionDenied

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SHORT_NAME = re.compile(r"~\d")


class ListFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(".", max_length=260, description="Folder to list, relative to the configured root.")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def resolve_inside(root: Path, rel: str) -> Path:
    """Map a user-supplied relative path to a real folder inside root, or raise."""
    if "\x00" in rel:
        raise PermissionDenied("Path contains a NUL character.")
    win = PureWindowsPath(rel)
    if win.drive or win.root or rel.startswith(("\\\\", "//")):
        raise PermissionDenied("Absolute paths, drive letters and UNC paths are not allowed; use a path relative to the root.")
    parts = [p for p in win.parts if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PermissionDenied("'..' is not allowed.")
        if ":" in part:
            raise PermissionDenied("':' is not allowed (alternate data streams).")
        if part.split(".")[0].rstrip(" ").lower() in _RESERVED:
            raise PermissionDenied(f"{part!r} is a reserved Windows device name.")
        if _SHORT_NAME.search(part):
            raise PermissionDenied(f"{part!r} looks like an 8.3 short name; use the full folder name.")

    current = root
    for part in parts:
        current = current / part
        if _is_link(current):
            raise PermissionDenied(f"{part!r} is a symbolic link or junction; links are not followed.")
    try:
        real = current.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFound(f"No folder named {rel!r} under the root.") from exc
    if not real.is_relative_to(root):
        raise PermissionDenied("Path resolves outside the configured root.")
    if not real.is_dir():
        raise InvalidInput(f"{rel!r} is a file, not a folder.")
    return real


class ListFiles:
    input_model = ListFilesInput

    def __init__(self, root: Path, max_entries: int = 500) -> None:
        if _is_link(root):
            raise ValueError(f"capability root must not be a link: {root}")
        self.root = root.resolve(strict=True)
        self.max_entries = max_entries
        self.manifest = CapabilityManifest(
            id="fs.list_files",
            name="List files",
            description="List the files and folders in one folder inside the configured sandbox root. Read-only.",
            version="0.1.0",
            permissions=(f"fs.read:{self.root.name}", "net.none", "exec.none"),
            timeout_s=5.0,
            max_output_bytes=256_000,
        )

    def run(self, args: ListFilesInput) -> dict[str, Any]:
        folder = resolve_inside(self.root, args.path)
        entries: list[dict[str, Any]] = []
        truncated = False
        with os.scandir(folder) as it:
            for entry in it:
                if len(entries) >= self.max_entries:
                    truncated = True
                    break
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction():
                    entries.append({"name": entry.name, "type": "link", "size": None})
                elif entry.is_dir(follow_symlinks=False):
                    entries.append({"name": entry.name, "type": "dir", "size": None})
                else:
                    entries.append({"name": entry.name, "type": "file", "size": entry.stat(follow_symlinks=False).st_size})
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        rel = folder.relative_to(self.root).as_posix()
        return {"root": self.root.name, "path": "." if rel == "." else rel, "entries": entries, "truncated": truncated}
