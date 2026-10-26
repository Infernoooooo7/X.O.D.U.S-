"""Path confinement for capability arguments (master prompt section 8, path traversal row).

Every model- or user-supplied path is relative to a configured root. Rules, checked before the
filesystem is touched and again on the resolved path:
  - no drive letters, rooted, UNC or device paths (\\\\server\\share, \\\\?\\C:\\, \\\\.\\pipe);
  - no '..', no ':' (alternate data streams), no wildcard or control characters;
  - no component ending in '.' or ' ' (Windows silently strips them, which defeats name checks);
  - no reserved device names (CON, NUL, COM1, LPT1, ... also with an extension such as nul.txt);
  - no 8.3 short-name components (PROGRA~1);
  - no symbolic link or junction anywhere on the way (links are never followed);
  - the resolved path must stay inside the resolved root.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PureWindowsPath

_RESERVED = {
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"com{i}" for i in "0123456789¹²³"),
    *(f"lpt{i}" for i in "0123456789¹²³"),
}
_SHORT_NAME = re.compile(r"~\d")
_BAD_CHARS = set('<>"|?*') | {chr(c) for c in range(32)}
MAX_PATH_CHARS = 260


class PathViolation(Exception):
    """The path is not allowed; the message is meant for the user."""

    code = "path_violation"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PathNotFound(PathViolation):
    code = "not_found"


class WrongKind(PathViolation):
    code = "invalid_input"


class WriteCancelled(Exception):
    code = "cancelled"


def split_relative(rel: object) -> list[str]:
    """Check a relative path syntactically and return its components ([] means the root itself)."""
    if not isinstance(rel, str):
        raise PathViolation("Path must be a string.")
    if len(rel) > MAX_PATH_CHARS:
        raise PathViolation(f"Path is longer than {MAX_PATH_CHARS} characters.")
    if "\x00" in rel:
        raise PathViolation("Path contains a NUL character.")
    win = PureWindowsPath(rel)
    if win.drive or win.root or rel.startswith(("\\\\", "//")):
        raise PathViolation("Absolute paths, drive letters and UNC paths are not allowed; use a path relative to the root.")
    parts = [p for p in win.parts if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PathViolation("'..' is not allowed.")
        if ":" in part:
            raise PathViolation("':' is not allowed (alternate data streams).")
        if any(ch in _BAD_CHARS for ch in part):
            raise PathViolation(f"{part!r} contains characters that are not allowed in Windows paths.")
        if part != part.rstrip(". "):
            raise PathViolation(f"{part!r} ends with a dot or space, which Windows silently strips.")
        if part.split(".")[0].rstrip(" ").lower() in _RESERVED:
            raise PathViolation(f"{part!r} is a reserved Windows device name.")
        if _SHORT_NAME.search(part):
            raise PathViolation(f"{part!r} looks like an 8.3 short name; use the full name.")
    return parts


def is_link(path: Path) -> bool:
    """True for symbolic links and junctions. Fails closed: an unreadable entry counts as a link."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def _walk(root: Path, parts: list[str]) -> Path:
    current = root
    for part in parts:
        current = current / part
        if is_link(current):
            raise PathViolation(f"{part!r} is a symbolic link or junction; links are not followed.")
    return current


def _real_root(root: Path) -> Path:
    if is_link(root):
        raise PathViolation("The configured root is itself a link; refusing to use it.")
    return root.resolve(strict=True)


def resolve_existing(root: Path, rel: object, kind: str = "dir") -> Path:
    """Map a relative path to an existing folder (kind='dir') or regular file (kind='file') inside root."""
    parts = split_relative(rel)
    real_root = _real_root(root)
    candidate = _walk(real_root, parts)
    try:
        real = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PathNotFound(f"Nothing named {rel!r} exists under the root.") from exc
    if not real.is_relative_to(real_root):
        raise PathViolation("Path resolves outside the configured root.")
    if kind == "dir" and not real.is_dir():
        raise WrongKind(f"{rel!r} is a file, not a folder.")
    if kind == "file" and not real.is_file():
        raise WrongKind(f"{rel!r} is not a regular file.")
    return real


def resolve_new_file(root: Path, rel: object) -> Path:
    """A file to create or replace: its folder must exist inside root; it must not be a link or a folder."""
    parts = split_relative(rel)
    if not parts:
        raise PathViolation("A file name is required.")
    folder = resolve_existing(root, "\\".join(parts[:-1]) or ".", "dir")
    target = folder / parts[-1]
    if is_link(target):
        raise PathViolation(f"{parts[-1]!r} is a symbolic link or junction; links are not followed.")
    if target.is_dir():
        raise WrongKind(f"{rel!r} is a folder, not a file.")
    return target


def resolve_path(root: Path, rel: object, kind: str) -> Path:
    if kind == "new_file":
        return resolve_new_file(root, rel)
    return resolve_existing(root, rel, kind)


def atomic_write_bytes(
    target: Path,
    data: bytes,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    chunk_size: int = 64 * 1024,
) -> None:
    """Write through a temporary file in the same folder, then rename it over the target.

    Cancellation (checked between chunks and before the rename) or any error removes the temporary
    file, so the target is either completely replaced or left untouched.
    """
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.xodus-tmp")
    try:
        with open(tmp, "xb") as fh:
            for start in range(0, len(data), chunk_size):
                if cancelled():
                    raise WriteCancelled("Write cancelled; the target was not changed.")
                fh.write(data[start : start + chunk_size])
            fh.flush()
            os.fsync(fh.fileno())
        if cancelled():
            raise WriteCancelled("Write cancelled; the target was not changed.")
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise
