"""Cascading deletion, verified three ways: row counts, index integrity, and a scan of the raw database files."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from tests.kb_helpers import DIM, HashEmbedder, hash_vector, topic_markdown
from xodus.ingestion.pipeline import PipelineDeps, Progress, RootInfo, process_job
from xodus.memory.manager import MemoryManager
from xodus.retrieval.hybrid import Retriever
from xodus.security.audit import AuditLog
from xodus.storage.knowledge import Conflict, NotFound
from xodus.storage.memory import MemoryStore
from xodus.storage.sqlite import Database, connect
from xodus.storage import migrate

GONE = "zxqcanaryalpha9001"
KEEP = "keepcanarybeta7002"


def raw_bytes(db_path: Path) -> bytes:
    parts = [db_path.read_bytes()]
    for suffix in ("-wal",):
        wal = Path(str(db_path) + suffix)
        if wal.exists():
            parts.append(wal.read_bytes())
    return b"".join(parts)


def ingest_file(store, deps, collection_id: int, rel: str) -> dict:
    """Queue, claim, process and finish one job, as the worker does (a finished job frees the file to be queued again)."""
    store.enqueue_job(collection_id, "docs", rel)
    job = store.claim_next_job(deps.owner)
    result = process_job(deps, job)
    store.finish_job(job["id"], deps.owner, "completed", outcome=result["outcome"], document_id=result["document_id"], chunks_done=result["chunks"])
    return result

def doc_text(canary: str, topic_words: str) -> str:
    return f"# {canary} report\n\n" + "\n\n".join(f"Paragraph {i} mentions {canary} and {topic_words}. " * 6 for i in range(40))


@pytest.fixture
def two_docs(kb_store, docs_root):
    deps = PipelineDeps(kb_store.settings, kb_store, HashEmbedder(), {"docs": RootInfo(docs_root, frozenset({"fs.read"}))}, "t:1", Progress(),
                        lambda s: None)
    coll = kb_store.create_collection("main")
    out = {"coll": coll, "deps": deps}
    for name, canary, words in (("gone.md", GONE, "telescopes and nebulae"), ("keep.md", KEEP, "oboes and violins")):
        (docs_root / name).write_text(doc_text(canary, words), encoding="utf-8")
        result = ingest_file(kb_store, deps, coll["id"], name)
        out[name] = {"id": result["document_id"], "chunks": [c["id"] for c in kb_store.list_chunks(result["document_id"], 200)]}
    kb_store.checkpoint()
    return out


def first_vector_bytes(store, docs_root, name: str) -> bytes:
    """The stored embedding of a document's first chunk, as it sits in the file."""
    doc = store.find_document(1, "docs", name)
    chunk = store.list_chunks(doc["id"], 1)[0]
    return struct.pack(f"{DIM}f", *hash_vector((chunk["heading_path"] + "\n" + chunk["text"]) if chunk["heading_path"] else chunk["text"], DIM))


def test_the_scan_can_see_what_it_is_looking_for(kb_store, two_docs, docs_root):
    raw = raw_bytes(kb_store.db.path)
    assert GONE.encode() in raw and KEEP.encode() in raw  # both canaries are in the file before any deletion
    assert first_vector_bytes(kb_store, docs_root, "gone.md") in raw  # and so is the embedding of the first chunk


def test_deleting_a_document_removes_it_from_every_table_and_index(kb_store, two_docs):
    gone = two_docs["gone.md"]
    assert kb_store.fts_candidates(f'"{GONE}"', [two_docs["coll"]["id"]], 5)  # searchable before
    report = kb_store.delete_document(gone["id"])
    assert report["chunks"] == len(gone["chunks"]) > 3 and report["vectors"] == report["chunks"] and report["checkpoint_complete"]
    check = kb_store.verify_gone(gone["id"], report["chunk_ids"], [GONE])
    assert check == {"documents": 0, "chunks": 0, "memory_items": 0, "jobs_pointing_at_it": 0, "vectors": 0, "fts_hits": 0,
                     "fts_index_consistent": True, "clean": True}
    assert kb_store.fts_candidates(f'"{GONE}"', [two_docs["coll"]["id"]], 5) == []
    assert kb_store.get_chunks(gone["chunks"]) == {}
    with kb_store.db.connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM chunk_vectors_rowids WHERE rowid IN (%s)" % ",".join("?" * len(gone["chunks"])),
                            gone["chunks"]).fetchone()[0] == 0


def test_the_other_document_is_untouched(kb_store, two_docs):
    keep = two_docs["keep.md"]
    before = [(c["id"], c["text"]) for c in kb_store.list_chunks(keep["id"], 200)]
    kb_store.delete_document(two_docs["gone.md"]["id"])
    assert [(c["id"], c["text"]) for c in kb_store.list_chunks(keep["id"], 200)] == before
    assert kb_store.fts_candidates(f'"{KEEP}"', [two_docs["coll"]["id"]], 50)
    assert kb_store.vector_candidates(hash_vector("oboes and violins", DIM), [two_docs["coll"]["id"]], 5)
    stats = kb_store.stats()
    assert stats["documents"] == {"ready": 1} and stats["chunks"] == stats["vectors"] == len(keep["chunks"])


def test_raw_database_files_no_longer_contain_the_deleted_text_or_vectors(kb_store, two_docs, docs_root):
    gone_vec = first_vector_bytes(kb_store, docs_root, "gone.md")
    kb_store.delete_document(two_docs["gone.md"]["id"])
    raw = raw_bytes(kb_store.db.path)
    assert GONE.encode() not in raw and GONE.encode("utf-16-le") not in raw
    assert b"telescopes and nebulae" not in raw and gone_vec not in raw
    assert KEEP.encode() in raw  # the scan still finds what should still be there


@pytest.mark.parametrize("pragma_on, fts_on, text_survives", [
    (True, True, False),    # what XODUS uses: the text is gone from the file
    (True, False, True),    # SQLite's secure_delete alone is not enough: the FTS index segments still hold the words
    (False, True, True),    # FTS5's secure-delete alone is not enough: freed pages are not overwritten
    (False, False, True),   # no protection at all
])
def test_both_delete_protections_are_needed_and_the_scan_can_tell_the_difference(tmp_path, pragma_on, fts_on, text_survives):
    """The raw-file scan is not vacuous: it finds deleted text when a protection is missing. Measured on this machine
    (SQLite 3.50.4): neither PRAGMA secure_delete nor FTS5 'secure-delete' removes the text alone; both together do.
    Deleted vectors never survive, because sqlite-vec clears them itself."""
    import sqlite_vec

    conn = connect(tmp_path / "control.db")
    migrate.migrate(conn, DIM)
    conn.execute(f"PRAGMA secure_delete = {'ON' if pragma_on else 'OFF'}")
    conn.execute(f"INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('secure-delete', {1 if fts_on else 0})")
    conn.execute("INSERT INTO collections(name, created_at) VALUES ('c', 0)")
    conn.execute("INSERT INTO documents(collection_id, source_root, source_path, title, format, size_bytes, content_hash, chunker_version,"
                 " status, created_at, updated_at) VALUES (1,'r','p','t','text',1,'h',1,'ready',0,0)")
    vec = [0.123 + i / 1000 for i in range(DIM)]
    packed = struct.pack(f"{DIM}f", *vec)
    for i in range(40):
        text = f"chunk {i} with {GONE} " + "filler " * 40
        conn.execute("INSERT INTO chunks(document_id, ordinal, text, text_hash, token_estimate) VALUES (1, ?, ?, ?, 10)", (i, text, f"h{i}"))
        conn.execute("INSERT INTO chunk_vectors(rowid, embedding, collection_id) VALUES (?, ?, 1)", (i + 1, sqlite_vec.serialize_float32(vec)))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = raw_bytes(tmp_path / "control.db")
    assert GONE.encode() in before and packed in before  # both are really in the file to begin with
    for i in range(40):
        conn.execute("DELETE FROM chunk_vectors WHERE rowid = ?", (i + 1,))
    conn.execute("DELETE FROM documents WHERE id = 1")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = raw_bytes(tmp_path / "control.db")
    conn.close()
    assert (GONE.encode() in after) is text_survives
    assert packed not in after


def test_xodus_connections_have_both_protections_switched_on(kb_store):
    with kb_store.db.connection() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        config = {row[0]: row[1] for row in conn.execute("SELECT k, v FROM chunks_fts_config")}
    assert str(config["secure-delete"]) == "1"


def test_derived_memory_is_deleted_with_its_document_but_other_memory_is_not(kb_store, two_docs, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    memory = MemoryManager(MemoryStore(kb_store.db), audit)
    gone_id, keep_id = two_docs["gone.md"]["id"], two_docs["keep.md"]["id"]
    derived = memory.propose("fact", "The nebula is far away.", source=f"document:{gone_id}", model="m", prompt_version="p1", document_id=gone_id)
    approved = memory.decide(derived["id"], "approve")
    other_doc = memory.propose("fact", "Oboes tune the orchestra.", source=f"document:{keep_id}", model="m", prompt_version="p1", document_id=keep_id)
    note = memory.create_user_item("note", "Remember to water the plants.")
    assert approved["status"] == "approved"
    report = kb_store.delete_document(gone_id)
    assert report["memory_items"] == 1
    remaining = {i["id"] for i in memory.store.list(limit=200)}
    assert remaining == {other_doc["id"], note["id"]} and derived["id"] not in remaining


def test_deleting_a_collection_removes_everything_in_it(kb_store, two_docs, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    memory = MemoryManager(MemoryStore(kb_store.db), audit)
    memory.propose("fact", "derived from keep", source=f"document:{two_docs['keep.md']['id']}", model="m", prompt_version="p", document_id=two_docs["keep.md"]["id"])
    other = kb_store.create_collection("other")
    report = kb_store.delete_collection(two_docs["coll"]["id"])
    assert report["documents"] == 2 and report["chunks"] == report["vectors"] > 6 and report["memory_items"] == 1
    stats = kb_store.stats()
    assert stats["documents"] == {} and stats["chunks"] == 0 == stats["vectors"] and stats["collections"] == 1
    assert kb_store.collection_by_name("other")["id"] == other["id"]
    raw = raw_bytes(kb_store.db.path)
    assert GONE.encode() not in raw and KEEP.encode() not in raw


def test_job_rows_keep_their_history_but_lose_the_pointer(kb_store, two_docs):
    job = kb_store.list_jobs(None, 10, 0)[0]
    assert job["document_id"] is None or job["document_id"] in (two_docs["gone.md"]["id"], two_docs["keep.md"]["id"])
    kb_store.delete_document(two_docs["gone.md"]["id"])
    with kb_store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_jobs").fetchone()[0] == 2  # history is kept
        assert conn.execute("SELECT count(*) FROM ingestion_jobs WHERE document_id = ?", (two_docs["gone.md"]["id"],)).fetchone()[0] == 0


def test_deleting_something_that_does_not_exist(kb_store, two_docs):
    with pytest.raises(NotFound):
        kb_store.delete_document(9999)
    with pytest.raises(NotFound):
        kb_store.delete_collection(9999)
    kb_store.delete_document(two_docs["gone.md"]["id"])
    with pytest.raises(NotFound):
        kb_store.delete_document(two_docs["gone.md"]["id"])  # already gone


async def test_a_deleted_chunk_is_never_returned_and_its_id_is_never_reused(kb_store, two_docs, docs_root):
    old_ids = set(two_docs["gone.md"]["chunks"])
    kb_store.delete_document(two_docs["gone.md"]["id"])

    async def embed(text):
        return hash_vector(text, DIM)

    r = Retriever(kb_store, kb_store.settings.knowledge, kb_store.settings.embedding, embed)
    result = await r.search("telescopes and nebulae", [two_docs["coll"]["id"]], k=20)
    assert not old_ids & {e.chunk_id for e in result.evidence}
    (docs_root / "gone.md").write_text(doc_text(GONE, "telescopes and nebulae"), encoding="utf-8")  # the same file comes back
    again = ingest_file(kb_store, two_docs["deps"], two_docs["coll"]["id"], "gone.md")
    new_ids = {c["id"] for c in kb_store.list_chunks(again["document_id"], 200)}
    assert new_ids and not new_ids & old_ids and min(new_ids) > max(old_ids)  # old citations cannot point at the new text
    assert again["document_id"] != two_docs["gone.md"]["id"]


def test_repeated_delete_and_reingest_keeps_the_indexes_consistent(kb_store, two_docs, docs_root):
    for round_ in range(4):
        doc = kb_store.find_document(two_docs["coll"]["id"], "docs", "gone.md")
        if doc:
            kb_store.delete_document(doc["id"])
        (docs_root / "gone.md").write_text(doc_text(GONE, f"round {round_} words"), encoding="utf-8")
        ingest_file(kb_store, two_docs["deps"], two_docs["coll"]["id"], "gone.md")
    stats = kb_store.stats()
    assert stats["chunks"] == stats["vectors"]
    with kb_store.db.connection() as conn:
        conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")  # raises if the index disagrees
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert b"round 0 words" not in raw_bytes(kb_store.db.path)


def test_the_api_level_checks_exist():
    assert Conflict.code == "conflict" and Database
