"""Migrations (up and down, checksums), connection settings, and schema-level invariants."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from xodus.storage import migrate as m
from xodus.storage.knowledge import ChunkRecord, KnowledgeStore
from xodus.storage.sqlite import Database, EmbeddingDimensionMismatch, MigrationError, connect

DIM = 8
TABLES = {"meta", "collections", "documents", "chunks", "chunks_fts", "chunk_vectors", "ingestion_jobs", "memory_items",
          "retrieval_log", "schema_migrations", "conversations", "messages", "model_switches"}
VERSIONS = [m.version for m in m.load_migrations()]


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_up_creates_every_table_and_records_the_migration(tmp_path):
    db = Database(tmp_path / "t.db", embedding_dim=DIM)
    assert db.open() == VERSIONS == [1, 2]
    with db.connection() as conn:
        assert TABLES <= tables(conn)
        assert m.applied(conn).keys() == {1, 2}
        assert conn.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()[0] == str(DIM)
    assert db.open() == []  # idempotent: nothing pending the second time


def test_connection_settings(tmp_path):
    conn = connect(tmp_path / "t.db", cache_kib=2048)
    m.migrate(conn, DIM)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -2048
    assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
    assert conn.execute("SELECT vec_version()").fetchone()[0].startswith("v")
    conn.close()


def test_down_removes_everything_and_up_can_be_applied_again(tmp_path):
    conn = connect(tmp_path / "t.db")
    m.migrate(conn, DIM)
    assert m.rollback(conn, 0) == [2, 1]  # newest first
    remaining = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')")}
    assert {n for n in remaining if not n.startswith("sqlite_")} == {"schema_migrations"}  # only the bookkeeping table is left
    assert m.migrate(conn, DIM) == [1, 2]
    assert m.rollback(conn, 1) == [2] and m.current_version(conn) == 1  # 0002 alone can be undone
    assert not {"conversations", "messages", "model_switches"} & tables(conn) and "memory_items" in tables(conn)
    assert conn.execute("SELECT count(*) FROM meta WHERE key = 'ingestion_paused'").fetchone()[0] == 0
    assert m.migrate(conn, DIM) == [2]
    conn.close()


def test_edited_migration_is_refused(tmp_path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    shutil.copy(m.MIGRATIONS_DIR / "0001_initial.sql", folder / "0001_initial.sql")
    conn = connect(tmp_path / "t.db")
    m.migrate(conn, DIM, folder)
    (folder / "0001_initial.sql").write_text((folder / "0001_initial.sql").read_text() + "\n-- sneaky edit\n")
    with pytest.raises(MigrationError, match="edited after it was applied"):
        m.migrate(conn, DIM, folder)
    conn.close()


def test_unknown_migration_in_database_is_refused(tmp_path):
    conn = connect(tmp_path / "t.db")
    m.migrate(conn, DIM)
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'from_the_future', 'x', 0)")
    with pytest.raises(MigrationError, match="does not know"):
        m.migrate(conn, DIM)
    conn.close()


def test_failed_migration_rolls_back_completely(tmp_path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "0001_bad.sql").write_text("-- +up\nCREATE TABLE a(x);\nCREATE TABLE a(y);\n-- +down\nDROP TABLE a;\n")
    conn = connect(tmp_path / "t.db")
    with pytest.raises(MigrationError, match="rolled back"):
        m.migrate(conn, DIM, folder)
    assert "a" not in tables(conn) and m.current_version(conn) == 0  # the first CREATE did not survive
    conn.close()


@pytest.mark.parametrize("name, body, message", [
    ("0001_nomarkers.sql", "CREATE TABLE a(x);", "needs a '-- \\+up' line"),
    ("0002_gap.sql", "-- +up\nSELECT 1;\n-- +down\nSELECT 1;\n", "without gaps"),
    ("bad_name.sql", "-- +up\n-- +down\n", "NNNN_name"),
])
def test_malformed_migration_files(tmp_path, name, body, message):
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / name).write_text(body)
    with pytest.raises(MigrationError, match=message):
        m.load_migrations(folder)


def test_split_statements_keeps_trigger_bodies_together():
    sql = "CREATE TABLE t(a);\nCREATE TRIGGER x AFTER INSERT ON t BEGIN\n  INSERT INTO t VALUES (1);\n  SELECT 2;\nEND;\n-- c\nSELECT 3;"
    parts = m.split_statements(sql)
    assert len(parts) == 3 and parts[1].startswith("CREATE TRIGGER") and parts[1].endswith("END;")


def test_embedding_dimension_mismatch_is_refused_with_advice(tmp_path):
    Database(tmp_path / "t.db", embedding_dim=8).open()
    with pytest.raises(EmbeddingDimensionMismatch, match="re-ingest"):
        Database(tmp_path / "t.db", embedding_dim=16).open()


def test_worker_refuses_an_unmigrated_database(tmp_path):
    with pytest.raises(MigrationError, match="needs"):
        Database(tmp_path / "empty.db", embedding_dim=DIM).open(apply_migrations=False)


# ---------------------------------------------------------------------------------------------- schema invariants


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "t.db", embedding_dim=DIM)
    db.open()
    yield KnowledgeStore(db)
    db.close()


def _doc(store, name="d.txt", collection=None):
    collection = collection or store.create_collection("c" + name.replace(".", "") + str(time.time_ns())[-6:])
    return collection, store.start_document(collection_id=collection["id"], root="r", path=name, title=name, fmt="text",
                                            size_bytes=1, content_hash="h", chunker_version=1, encoding="utf-8")


def _rec(i, text="hello world"):
    return ChunkRecord(i, text, f"hash{i}", "H", None, None, 0, len(text), 3)


def test_chunks_are_immutable(store):
    col, doc = _doc(store)
    store.append_chunks(doc["id"], col["id"], [_rec(0)], [[0.1] * DIM])
    with store.db.connection() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE chunks SET text = 'changed' WHERE document_id = ?", (doc["id"],))


def test_append_checks_checkpoint_status_dimension_and_limit(store):
    from xodus.storage.knowledge import Conflict, CorpusLimitExceeded
    from xodus.storage.migrate import StorageError

    col, doc = _doc(store)
    with pytest.raises(Conflict, match="checkpoint"):
        store.append_chunks(doc["id"], col["id"], [_rec(5)], [[0.1] * DIM])  # gap: would skip chunks 0-4
    with pytest.raises(StorageError, match="dimensions"):
        store.append_chunks(doc["id"], col["id"], [_rec(0)], [[0.1] * (DIM + 1)])
    store.append_chunks(doc["id"], col["id"], [_rec(0)], [[0.1] * DIM])
    with pytest.raises(Conflict, match="checkpoint"):
        store.append_chunks(doc["id"], col["id"], [_rec(0)], [[0.1] * DIM])  # replay of an already stored batch
    store.finish_document(doc["id"])
    with pytest.raises(Conflict, match="not ingesting"):
        store.append_chunks(doc["id"], col["id"], [_rec(1)], [[0.1] * DIM])
    store.max_chunks = 1
    col2, doc2 = _doc(store, "e.txt")
    with pytest.raises(CorpusLimitExceeded, match="limit of 1"):
        store.append_chunks(doc2["id"], col2["id"], [_rec(0)], [[0.1] * DIM])


def test_failed_batch_leaves_no_partial_rows(store):
    col, doc = _doc(store)
    records = [_rec(0), _rec(0, "duplicate ordinal")]  # the second insert violates UNIQUE(document_id, ordinal)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_chunks(doc["id"], col["id"], records, [[0.1] * DIM, [0.2] * DIM])
    stats = store.stats()
    assert stats["chunks"] == 0 and stats["vectors"] == 0
    assert store.get_document(doc["id"])["chunks_done"] == 0


def test_status_and_format_constraints(store):
    col = store.create_collection("x")
    with store.db.connection() as conn:
        for sql in (
            "INSERT INTO documents(collection_id,source_root,source_path,title,format,size_bytes,content_hash,chunker_version,status,created_at,updated_at)"
            " VALUES (?, 'r','p','t','exe',1,'h',1,'ready',0,0)",
            "INSERT INTO documents(collection_id,source_root,source_path,title,format,size_bytes,content_hash,chunker_version,status,created_at,updated_at)"
            " VALUES (?, 'r','p','t','text',1,'h',1,'hacked',0,0)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                conn.execute(sql, (col["id"],))


def test_memory_constraints_enforce_provenance_and_decision(store):
    with store.db.connection() as conn:
        base = "INSERT INTO memory_items(kind,content,status,created_by,source,model,prompt_version,created_at,updated_at,decided_at) VALUES "
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a model-derived item needs model and prompt version
            conn.execute(base + "('fact','x','proposed','model','task:ab',NULL,NULL,0,0,NULL)")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a model item cannot be approved without a decision time
            conn.execute(base + "('fact','x','approved','model','task:ab','m','p',0,0,NULL)")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(base + "('fact','','approved','user','manual',NULL,NULL,0,0,0)")
        conn.execute(base + "('fact','x','proposed','model','task:ab','m','p',0,0,NULL)")


def test_unique_collection_and_document_paths(store):
    from xodus.storage.knowledge import Conflict

    store.create_collection("same")
    with pytest.raises(Conflict, match="already exists"):
        store.create_collection("same")


def test_ids_are_never_reused_after_deletion(store):
    """A citation like [chunk:3] must never later mean different text, so deleted ids stay retired."""
    col, doc = _doc(store, "ids.txt")
    first = store.append_chunks(doc["id"], col["id"], [_rec(0), _rec(1), _rec(2)], [[0.1] * DIM] * 3)
    store.finish_document(doc["id"])
    store.delete_document(doc["id"])
    col2, doc2 = _doc(store, "ids2.txt", col)
    second = store.append_chunks(doc2["id"], col2["id"], [_rec(0), _rec(1)], [[0.1] * DIM] * 2)
    assert not set(first) & set(second) and min(second) > max(first)
    assert doc2["id"] > doc["id"]
    job = store.enqueue_job(col2["id"], "r", "x"); store.request_cancel(job["id"])
    with store.db.connection() as conn:
        conn.execute("DELETE FROM ingestion_jobs")
    assert store.enqueue_job(col2["id"], "r", "y")["id"] > job["id"]


def test_start_document_replaces_chunks_of_a_changed_file(store):
    col, doc = _doc(store, "f.txt")
    store.append_chunks(doc["id"], col["id"], [_rec(0), _rec(1)], [[0.1] * DIM, [0.2] * DIM])
    store.finish_document(doc["id"])
    again = store.start_document(collection_id=col["id"], root="r", path="f.txt", title="f", fmt="text", size_bytes=2,
                                 content_hash="h2", chunker_version=1, encoding="utf-8")
    assert again["id"] == doc["id"] and again["status"] == "ingesting" and again["chunks_done"] == 0
    assert store.stats()["chunks"] == 0 and store.stats()["vectors"] == 0
