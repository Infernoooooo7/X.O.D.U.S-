"""fs.list_files: list one folder inside a configured root. Read-only; never follows links.

The executor confines the path argument before this runs (it is declared with x-xodus-path); run()
resolves it again with the same rules as defence in depth and to narrow the check-to-use window.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from xodus.capabilities.manifest import CapabilityManifest
from xodus.capabilities.registry import (
    CapabilityCancelled,
    InProcessCapability,
    InvalidInput,
    InvocationContext,
    NotFound,
    PermissionDenied,
)
from xodus.security.paths import PathNotFound, PathViolation, WrongKind, is_link, resolve_existing


class ListFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(".", max_length=260, description="Folder to list, relative to the configured root.")


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["root", "path", "entries", "truncated"],
    "properties": {
        "root": {"type": "string"},
        "path": {"type": "string"},
        "truncated": {"type": "boolean"},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "size"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"enum": ["file", "dir", "link"]},
                    "size": {"type": ["integer", "null"]},
                },
            },
        },
    },
}


def capability_error(exc: PathViolation) -> Exception:
    if isinstance(exc, PathNotFound):
        return NotFound(exc.message)
    if isinstance(exc, WrongKind):
        return InvalidInput(exc.message)
    return PermissionDenied(exc.message)


class ListFiles(InProcessCapability):
    def __init__(self, root_id: str, root: Path, max_entries: int = 500) -> None:
        if is_link(root):
            raise ValueError(f"capability root must not be a link: {root}")
        self.root = root.resolve(strict=True)
        self.root_id = root_id
        self.max_entries = max_entries
        schema = ListFilesInput.model_json_schema()
        schema["properties"]["path"].update({"x-xodus-path": f"fs.read:{root_id}", "x-xodus-path-kind": "dir"})
        self.manifest = CapabilityManifest(
            id="fs.list_files",
            name="List files",
            description="List the files and folders in one folder inside the configured sandbox root. Read-only.",
            version="0.2.0",
            input_schema=schema,
            output_schema=OUTPUT_SCHEMA,
            permissions=(f"fs.read:{root_id}", "net.none", "exec.none"),
            execution_mode="in_process",
            process_tier="none",
            resource_class="light",
            timeout_s=5.0,
            max_output_bytes=256_000,
            cancellable=True,
            requires_approval="never",
            side_effects="none",
            undoable=None,
        )

    def run(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        rel = args.get("path", ".")
        try:
            folder = resolve_existing(self.root, rel, "dir")
        except PathViolation as exc:
            raise capability_error(exc) from exc
        entries: list[dict[str, Any]] = []
        truncated = False
        with os.scandir(folder) as it:
            for entry in it:
                if ctx.cancel.is_set():
                    raise CapabilityCancelled("Listing cancelled.")
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
        rel_out = folder.relative_to(self.root).as_posix()
        return {"root": self.root.name, "path": "." if rel_out == "." else rel_out, "entries": entries, "truncated": truncated}
