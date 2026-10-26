"""Versioned SQL migrations (up and down), applied atomically and checksummed.

Files are storage/migrations/NNNN_name.sql with a "-- +up" and a "-- +down" section. An applied
migration whose file later changes is refused (its checksum is stored), so the schema a database was
built with can always be reproduced from the files.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class StorageError(Exception):
    code = "storage_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MigrationError(StorageError):
    code = "migration_error"


class EmbeddingDimensionMismatch(StorageError):
    code = "embedding_dimension_mismatch"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str
    checksum: str


def split_statements(sql: str) -> list[str]:
    """Split a script into statements; sqlite3.complete_statement understands trigger BEGIN...END bodies."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise MigrationError(f"Incomplete SQL statement at the end of a migration: {buffer.strip()[:80]!r}")
    return statements


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"Migration file {path.name!r} must be named NNNN_name.sql")
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        up = re.search(r"^-- \+up[ \t]*$", text, re.MULTILINE)  # markers sit alone on their line
        down = re.search(r"^-- \+down[ \t]*$", text, re.MULTILINE)
        if up is None or down is None or down.start() < up.end():
            raise MigrationError(f"{path.name} needs a '-- +up' line followed later by a '-- +down' line")
        found.append(Migration(
            version=int(match.group(1)), name=match.group(2),
            up=text[up.end():down.start()], down=text[down.end():],
            checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Migration versions must be 1..N without gaps, found {versions}")
    return found


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at REAL NOT NULL)")


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    _ensure_table(conn)
    return {int(r[0]): str(r[1]) for r in conn.execute("SELECT version, checksum FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    return max(applied(conn), default=0)


def _run(conn: sqlite3.Connection, statements: list[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def migrate(conn: sqlite3.Connection, embedding_dim: int, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration; return the versions applied. Safe to call from several processes."""
    migrations = load_migrations(directory)
    done = applied(conn)
    for version, checksum in done.items():
        known = next((m for m in migrations if m.version == version), None)
        if known is None:
            raise MigrationError(f"The database has migration {version}, which this version of XODUS does not know.")
        if known.checksum != checksum:
            raise MigrationError(f"Migration {version} ({known.name}) was edited after it was applied; "
                                 "add a new migration instead of changing an applied one.")
    applied_now: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if migration.version in applied(conn):  # another process won the race while we waited for the lock
                conn.execute("ROLLBACK")
                continue
            _run(conn, split_statements(migration.up.replace("{{EMBEDDING_DIM}}", str(int(embedding_dim)))))
            conn.execute("INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                         (migration.version, migration.name, migration.checksum, time.time()))
            conn.execute("COMMIT")
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise MigrationError(f"Migration {migration.version} ({migration.name}) failed and was rolled back: {exc}") from exc
            raise
        applied_now.append(migration.version)
    return applied_now


def rollback(conn: sqlite3.Connection, to_version: int, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Undo migrations above to_version (newest first). Destroys the data those migrations created."""
    migrations = {m.version: m for m in load_migrations(directory)}
    undone: list[int] = []
    for version in sorted(applied(conn), reverse=True):
        if version <= to_version:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            _run(conn, split_statements(migrations[version].down))
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
            conn.execute("COMMIT")
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise MigrationError(f"Rolling back migration {version} failed: {exc}") from exc
            raise
        undone.append(version)
    return undone
