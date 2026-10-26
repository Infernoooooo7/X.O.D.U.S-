"""Permission policy: which permissions are granted, and on which folders (master prompt 4.2).

Grants come only from configuration. Nothing a model outputs can add a root or a grant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

PERMISSION_FORMS = "fs.read:<root>, fs.write:<root>, kb.read:<collection>, net.none, net.outbound, exec.none, exec.spawn"
_FS = re.compile(r"^(fs\.read|fs\.write):([a-z][a-z0-9_-]{0,31})$")
_KB = re.compile(r"^(kb\.read):([a-z][a-z0-9_-]{0,31})$")
_PLAIN = {"net.none", "net.outbound", "exec.none", "exec.spawn"}
DECLARATIONS = {"net.none", "exec.none"}  # promises made by the capability; always satisfiable
ROOT_GRANTS = {"fs.read", "fs.write"}
COLLECTION_GRANTS = {"kb.read"}


def parse_permission(permission: str) -> tuple[str, str | None]:
    """Return (kind, root id or None); raise ValueError for anything outside the vocabulary."""
    if not isinstance(permission, str):
        raise ValueError("permission must be a string")
    match = _FS.match(permission) or _KB.match(permission)
    if match:
        return match.group(1), match.group(2)
    if permission in _PLAIN:
        return permission, None
    raise ValueError(f"unknown permission {permission!r} (allowed forms: {PERMISSION_FORMS})")


@dataclass(frozen=True)
class Root:
    id: str
    path: Path
    grants: frozenset[str]


class PermissionPolicy:
    def __init__(self, roots: Iterable[Root], global_grants: Iterable[str] = (),
                 collections: Mapping[str, Iterable[str]] | None = None) -> None:
        self.roots = {root.id: root for root in roots}
        self.global_grants = frozenset(global_grants)
        self.collections = {name: frozenset(grants) for name, grants in (collections or {}).items()}

    def readable_collections(self) -> list[str]:
        """Knowledge collections a model-invoked capability may read (names; deny by default)."""
        return sorted(name for name, grants in self.collections.items() if "kb.read" in grants)

    def missing(self, permissions: Iterable[str]) -> list[str]:
        """Permissions that are not granted, each with the reason."""
        out: list[str] = []
        for permission in permissions:
            try:
                kind, scope = parse_permission(permission)
            except ValueError as exc:
                out.append(str(exc))
                continue
            if kind in ROOT_GRANTS:
                root = self.roots.get(scope or "")
                if root is None:
                    out.append(f"{permission} (no root named {scope!r} is configured)")
                elif kind not in root.grants:
                    out.append(f"{permission} (root {scope!r} is not granted {kind})")
            elif kind in COLLECTION_GRANTS:
                grants = self.collections.get(scope or "")
                if grants is None:
                    out.append(f"{permission} (no collection grant for {scope!r} is configured)")
                elif kind not in grants:
                    out.append(f"{permission} (collection {scope!r} is not granted {kind})")
            elif kind in DECLARATIONS:
                continue
            elif kind not in self.global_grants:
                out.append(f"{permission} (not granted)")
        return out

    def root_for(self, permission: str) -> Root:
        kind, scope = parse_permission(permission)
        if kind not in ROOT_GRANTS or scope not in self.roots:
            raise KeyError(permission)
        return self.roots[scope]

    def describe(self) -> dict[str, object]:
        return {
            "roots": [{"id": r.id, "path": str(r.path), "grants": sorted(r.grants)} for r in self.roots.values()],
            "collections": {name: sorted(grants) for name, grants in self.collections.items()},
            "global_grants": sorted(self.global_grants),
        }
