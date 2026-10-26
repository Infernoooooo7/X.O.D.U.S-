"""SQLite access for XODUS: configured connections (FTS5 + sqlite-vec), a small pool, migrations.

Connection settings (all per connection, applied in connect()):
  WAL journal (readers never block the writer; the ingestion worker is a second writer process),
  foreign keys ON (cascades), secure_delete ON (deleted content is overwritten, not just unlinked),
  a bounded page cache (storage.cache_kib), memory-mapped I/O off (keeps the working set predictable).
Autocommit mode: transactions are explicit (transaction()).
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from xodus.storage.migrate import (
    EmbeddingDimensionMismatch,
    MigrationError,
    StorageError,
    current_version,
    load_migrations,
    migrate,
)

__all__ = ["Database", "EmbeddingDimensionMismatch", "MigrationError", "StorageError", "connect"]


def connect(path: Path | str, *, cache_kib: int = 16384, busy_timeout_ms: int = 10_000) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.Error, AttributeError) as exc:
        conn.close()
        raise StorageError(f"Could not load the sqlite-vec extension: {exc}") from exc
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute(f"PRAGMA cache_size = {-int(cache_kib)}")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA journal_size_limit = 67108864")
    return conn


class Database:
    """A file-backed database with a bounded connection pool. open() migrates and checks the schema."""

    def __init__(self, path: Path, *, embedding_dim: int, cache_kib: int = 16384, busy_timeout_ms: int = 10_000,
                 pool_size: int = 4) -> None:
        self.path = path
        self.embedding_dim = embedding_dim
        self._cache_kib = cache_kib
        self._busy_ms = busy_timeout_ms
        self._pool_size = pool_size
        self._idle: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def open(self, *, apply_migrations: bool = True) -> list[int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(self.path, cache_kib=self._cache_kib, busy_timeout_ms=self._busy_ms)
        try:
            applied = migrate(conn, self.embedding_dim) if apply_migrations else []
            expected = len(load_migrations())
            version = current_version(conn)
            if version != expected:
                raise MigrationError(f"The database schema is at version {version} but this XODUS needs {expected}; "
                                     "start the backend once so it can migrate the database.")
            self._check_embedding_dim(conn)
        finally:
            conn.close()
        return applied

    def _check_embedding_dim(self, conn: sqlite3.Connection) -> None:
        stored = conn.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()
        if stored is None or int(stored[0]) != self.embedding_dim:
            raise EmbeddingDimensionMismatch(
                f"The database stores {stored[0] if stored else 'unknown'}-dimensional embeddings but the configuration "
                f"says embedding.dimensions = {self.embedding_dim}. Set it back, or start a new database "
                "(different storage.filename) and re-ingest; vectors of different sizes cannot be mixed.")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any exception)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _acquire(self) -> sqlite3.Connection:
        if self._closed:
            raise StorageError("The database is closed.")
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._pool_size:
                self._created += 1
                try:
                    return connect(self.path, cache_kib=self._cache_kib, busy_timeout_ms=self._busy_ms)
                except BaseException:
                    self._created -= 1
                    raise
        try:
            return self._idle.get(timeout=self._busy_ms / 1000)
        except queue.Empty as exc:
            raise StorageError("All database connections are busy; try again.") from exc

    def _release(self, conn: sqlite3.Connection) -> None:
        if self._closed:
            conn.close()
            return
        if conn.in_transaction:  # never hand a connection with an open transaction to the next user
            conn.execute("ROLLBACK")
        self._idle.put(conn)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break

    def file_sizes(self) -> dict[str, int]:
        sizes = {}
        for suffix, key in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
            p = Path(str(self.path) + suffix)
            sizes[key] = p.stat().st_size if p.exists() else 0
        return sizes
