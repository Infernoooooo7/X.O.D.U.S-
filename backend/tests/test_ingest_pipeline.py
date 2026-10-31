"""The ingestion pipeline in-process: formats, idempotency, replacement, limits, resume, cancellation, outages."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.kb_helpers import DIM, HashEmbedder, topic_markdown, write_csv, write_docx, write_pdf, zip_with
from xodus.ingestion.chunking import CHUNKER_VERSION
from xodus.ingestion.pipeline import Cancelled, IngestError, PipelineDeps, Progress, RootInfo, process_job
from xodus.providers.base import ModelNotAvailable, ProviderUnavailable
from xodus.providers.ollama import EmbeddingDimensionError
from xodus.storage.knowledge import KnowledgeStore, LeaseLost

OWNER = "test:1"


class Boom(BaseException):
    """Simulates the process dying: not an Exception, so no cleanup code runs."""


def deps_for(store: KnowledgeStore, settings, docs: Path, embedder=None, *, knowledge: dict | None = None) -> PipelineDeps:
    if knowledge:
        settings = settings.model_copy(update={"knowledge": settings.knowledge.model_copy(update=knowledge)})
    return PipelineDeps(settings=settings, store=store, embedder=embedder or HashEmbedder(), roots={"docs": RootInfo(docs, frozenset({"fs.read"}))},
                        owner=OWNER, progress=Progress(), sleep=lambda _s: None)


@pytest.fixture
def d(kb_store, docs_root):
    return deps_for(kb_store, kb_store.settings, docs_root)


def run(deps: PipelineDeps, rel: str, collection: str = "main"):
    """Queue a file and process it like the worker does, including recording how the job ended."""
    store = deps.store
    coll = store.collection_by_name(collection) or store.create_collection(collection)
    store.enqueue_job(coll["id"], "docs", rel)
    job = store.claim_next_job(deps.owner)
    try:
        result = process_job(deps, job)
    except IngestError as exc:
        store.finish_job(job["id"], deps.owner, "failed", error_code=exc.code, error=exc.message)
        raise
    except Cancelled:
        store.finish_job(job["id"], deps.owner, "cancelled")
        raise
    store.finish_job(job["id"], deps.owner, "completed", outcome=result["outcome"], document_id=result["document_id"], chunks_done=result["chunks"])
    return job, result


def doc_of(store, rel: str, collection: str = "main") -> dict:
    return store.find_document(store.collection_by_name(collection)["id"], "docs", rel)


def chunk_texts(store, doc_id: int) -> list[tuple[int, str, str]]:
    return [(c["ordinal"], c["text"], c["heading_path"]) for c in store.list_chunks(doc_id, 200, 0)]


# ---------------------------------------------------------------------------------------------- formats


def test_markdown_is_ingested_with_headings_and_vectors(d, docs_root):
    (docs_root / "astro.md").write_text(topic_markdown("astronomy"), encoding="utf-8")
    _, result = run(d, "astro.md")
    assert result["outcome"] == "ingested" and result["chunks"] >= 3
    doc = doc_of(d.store, "astro.md")
    assert doc["status"] == "ready" and doc["format"] == "markdown" and doc["chunks_done"] == result["chunks"]
    chunks = d.store.list_chunks(doc["id"])
    assert all(c["heading_path"].startswith("Astronomy handbook") for c in chunks)
    stats = d.store.stats()
    assert stats["chunks"] == stats["vectors"] == result["chunks"]  # one vector per chunk, no strays


def test_plain_text_pdf_docx_and_csv(d, docs_root):
    (docs_root / "notes.txt").write_text("\n\n".join(topic_markdown("cooking").split("\n\n")[:14]), encoding="utf-8")
    write_pdf(docs_root / "paper.pdf", [("Gardening notes. " + "Plant tomatoes after frost and water deeply. " * 30)] * 3)
    write_docx(docs_root / "memo.docx", [("h1", "Finance memo"), ("p", "Compound interest grows investments. " * 40), ("h2", "Risk"),
                                          ("p", "Diversify across stocks and bonds. " * 40)], table=[["Fund", "Return"], ["Index", "7%"]])
    write_csv(docs_root / "rates.csv", ["bank", "rate"], [[f"Bank {i}", f"{i}.5%"] for i in range(120)])
    for rel in ("notes.txt", "paper.pdf", "memo.docx", "rates.csv"):
        _, result = run(d, rel)
        assert result["chunks"] >= 1, rel
        assert doc_of(d.store, rel)["status"] == "ready"
    pdf = doc_of(d.store, "paper.pdf")
    assert pdf["pages"] == 3 and {c["page"] for c in d.store.list_chunks(pdf["id"])} <= {1, 2, 3}
    assert any("Finance memo > Risk" == c["heading_path"] for c in d.store.list_chunks(doc_of(d.store, "memo.docx")["id"]))
    csv_chunks = d.store.list_chunks(doc_of(d.store, "rates.csv")["id"])
    assert csv_chunks[0]["locator"].startswith("row 2") and "bank: Bank 0" in csv_chunks[0]["text"]
    stats = d.store.stats()
    assert stats["chunks"] == stats["vectors"]
    assert stats["documents"] == {"ready": 4}


# ---------------------------------------------------------------------------------------------- idempotency and replacement


def test_unchanged_file_is_skipped_without_embedding(d, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music"), encoding="utf-8")
    _, first = run(d, "a.md")
    calls = d.embedder.total_texts
    _, second = run(d, "a.md")
    assert second["outcome"] == "unchanged" and second["document_id"] == first["document_id"]
    assert d.embedder.total_texts == calls  # nothing was embedded again
    assert d.store.stats()["chunks"] == first["chunks"]


def test_changed_file_replaces_the_old_document(d, docs_root):
    path = docs_root / "a.md"
    path.write_text(topic_markdown("music"), encoding="utf-8")
    _, first = run(d, "a.md")
    old_ids = {c["id"] for c in d.store.list_chunks(first["document_id"], 200)}
    path.write_text(topic_markdown("finance", sections=2), encoding="utf-8")
    _, second = run(d, "a.md")
    assert second["outcome"] == "replaced" and second["document_id"] == first["document_id"]  # same row, new content
    new_chunks = d.store.list_chunks(second["document_id"], 200)
    assert not old_ids & {c["id"] for c in new_chunks}  # the old chunks are gone, not mixed in
    assert all("orchestra" not in c["text"] and "Finance" in c["heading_path"] for c in new_chunks)
    stats = d.store.stats()
    assert stats["chunks"] == stats["vectors"] == len(new_chunks) and stats["documents"] == {"ready": 1}


def test_same_content_in_two_collections_is_two_documents(d, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music", 1), encoding="utf-8")
    run(d, "a.md", "one")
    run(d, "a.md", "two")
    assert d.store.stats()["documents"] == {"ready": 2}


def test_embedding_batches_respect_the_envelope(d, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("networking", 6, 4), encoding="utf-8")
    _, result = run(d, "a.md")
    sizes = [len(call) for call in d.embedder.calls]
    assert max(sizes) <= d.settings.embedding.batch_size == 4 and sum(sizes) == result["chunks"]


# ---------------------------------------------------------------------------------------------- resume


def _fresh_result(tmp_path, kb_settings, docs_root, text):
    """The chunks an uninterrupted run produces, from a separate database."""
    from xodus.storage.sqlite import Database

    settings = kb_settings()
    db = Database(tmp_path / "clean.db", embedding_dim=settings.embedding.dimensions)
    db.open()
    store = KnowledgeStore(db)
    deps = deps_for(store, settings, docs_root)
    _, result = run(deps, "big.md")
    chunks = chunk_texts(store, result["document_id"])
    db.close()
    return chunks


def test_resume_after_a_crash_matches_an_uninterrupted_run_and_skips_finished_batches(kb_store, kb_settings, docs_root, tmp_path):
    text = topic_markdown("astronomy", 8, 4)
    (docs_root / "big.md").write_text(text, encoding="utf-8")
    expected = _fresh_result(tmp_path, kb_settings, docs_root, text)
    assert len(expected) >= 9  # at least three batches of four

    class DiesOnThirdBatch(HashEmbedder):
        def embed(self, texts):
            if len(self.calls) == 2:
                raise Boom()
            return super().embed(texts)

    first = deps_for(kb_store, kb_store.settings, docs_root, DiesOnThirdBatch())
    coll = kb_store.create_collection("main")
    job = kb_store.enqueue_job(coll["id"], "docs", "big.md")
    job = kb_store.claim_next_job(OWNER)
    with pytest.raises(Boom):
        process_job(first, job)
    partial = doc_of(kb_store, "big.md")
    assert partial["status"] == "ingesting" and partial["chunks_done"] == 8  # two committed batches of four
    assert kb_store.stats()["chunks"] == 8 and kb_store.stats()["vectors"] == 8

    # the worker is gone: its job goes back to the queue, and a new worker resumes it
    assert kb_store.requeue_orphans(lambda owner: False, 3)["requeued"] == [job["id"]]
    second = deps_for(kb_store, kb_store.settings, docs_root, HashEmbedder())
    job2 = kb_store.claim_next_job("test:2")
    assert job2["attempts"] == 2
    second.owner = "test:2"
    result = process_job(second, job2)
    assert result["outcome"] == "ingested" and result["chunks"] == len(expected)
    assert second.embedder.total_texts == len(expected) - 8  # only the unfinished chunks were embedded
    assert chunk_texts(kb_store, partial["id"]) == expected  # identical chunks, ordinals and headings, no duplicates
    stats = kb_store.stats()
    assert stats["chunks"] == stats["vectors"] == len(expected)
    assert doc_of(kb_store, "big.md")["status"] == "ready"


def test_resume_restarts_cleanly_when_the_stored_chunks_no_longer_match(kb_store, docs_root):
    (docs_root / "big.md").write_text(topic_markdown("music", 6, 4), encoding="utf-8")
    d1 = deps_for(kb_store, kb_store.settings, docs_root)
    coll = kb_store.create_collection("main")

    class DiesOnSecondBatch(HashEmbedder):
        def embed(self, texts):
            if len(self.calls) == 1:
                raise Boom()
            return super().embed(texts)

    d1.embedder = DiesOnSecondBatch()
    kb_store.enqueue_job(coll["id"], "docs", "big.md")
    job = kb_store.claim_next_job(OWNER)
    with pytest.raises(Boom):
        process_job(d1, job)
    assert doc_of(kb_store, "big.md")["chunks_done"] == 4
    kb_store.requeue_orphans(lambda owner: False, 3)
    # a different chunk size on resume: the regenerated chunks differ from the stored ones
    d2 = deps_for(kb_store, kb_store.settings, docs_root, knowledge={"chunk_min_tokens": 100, "chunk_target_tokens": 150, "chunk_max_tokens": 200})
    d2.owner = "test:2"
    result = process_job(d2, kb_store.claim_next_job("test:2"))
    texts = chunk_texts(kb_store, result["document_id"])
    assert [o for o, _, _ in texts] == list(range(len(texts))) and len(texts) == result["chunks"]  # consecutive, no leftovers
    assert d2.embedder.total_texts == result["chunks"]  # restarted from zero with the new settings
    stats = kb_store.stats()
    assert stats["chunks"] == stats["vectors"] == result["chunks"]


def test_a_file_that_changed_after_the_crash_is_replaced_not_resumed(kb_store, docs_root):
    path = docs_root / "big.md"
    path.write_text(topic_markdown("music", 6, 4), encoding="utf-8")
    coll = kb_store.create_collection("main")

    class Dies(HashEmbedder):
        def embed(self, texts):
            if len(self.calls) == 1:
                raise Boom()
            return super().embed(texts)

    kb_store.enqueue_job(coll["id"], "docs", "big.md")
    with pytest.raises(Boom):
        process_job(deps_for(kb_store, kb_store.settings, docs_root, Dies()), kb_store.claim_next_job(OWNER))
    path.write_text(topic_markdown("gardening", 3, 4), encoding="utf-8")
    kb_store.requeue_orphans(lambda owner: False, 3)
    d2 = deps_for(kb_store, kb_store.settings, docs_root)
    d2.owner = "test:2"
    result = process_job(d2, kb_store.claim_next_job("test:2"))
    assert d2.embedder.total_texts == result["chunks"]  # everything re-embedded
    assert all("tomato" in t or "soil" in t or "roses" in t or "Gardening" in h or "garden" in t.lower()
               for _, t, h in chunk_texts(kb_store, result["document_id"]))


# ---------------------------------------------------------------------------------------------- limits and refusals


def refuse(deps, rel, code):
    with pytest.raises(IngestError) as err:
        run(deps, rel)
    assert err.value.code == code, err.value.message
    return err.value


def test_oversized_file_is_refused_before_any_document_exists(kb_store, docs_root):
    (docs_root / "big.txt").write_bytes(b"x " * 700_000)  # 1.4 MB
    d = deps_for(kb_store, kb_store.settings, docs_root, knowledge={"max_file_mb": 1})
    refuse(d, "big.txt", "file_too_large")
    assert kb_store.stats()["documents"] == {} and d.embedder.calls == []


def test_pdf_page_limit_unsupported_empty_binary_and_scanned(kb_store, docs_root):
    write_pdf(docs_root / "long.pdf", [f"Page {i} text" for i in range(6)])
    write_pdf(docs_root / "scan.pdf", ["", ""])
    (docs_root / "x.exe").write_bytes(b"MZ")
    (docs_root / "empty.txt").write_bytes(b"")
    (docs_root / "bin.txt").write_bytes(b"\x00\x01\x02" * 100)
    d = deps_for(kb_store, kb_store.settings, docs_root, knowledge={"max_pdf_pages": 5})
    refuse(d, "long.pdf", "too_many_pages")
    err = refuse(d, "scan.pdf", "no_text_layer")
    assert "OCR" in err.message
    for rel, code in (("x.exe", "unsupported_format"), ("empty.txt", "empty_file"), ("bin.txt", "binary_file")):
        refuse(d, rel, code)
    scan = doc_of(kb_store, "scan.pdf")
    assert scan["status"] == "failed" and "OCR" in scan["error"] and kb_store.stats()["chunks"] == 0  # visible, with its reason


def test_corpus_limit_fails_the_file_and_leaves_no_partial_chunks(kb_store, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    (docs_root / "b.md").write_text(topic_markdown("finance", 8, 4), encoding="utf-8")
    d = deps_for(kb_store, kb_store.settings, docs_root)
    _, first = run(d, "a.md")
    kb_store.max_chunks = first["chunks"] + 3
    err = refuse(d, "b.md", "corpus_limit")
    assert str(kb_store.max_chunks) in err.message
    assert kb_store.stats()["chunks"] == first["chunks"] == kb_store.stats()["vectors"]  # the failed file left nothing behind
    assert doc_of(kb_store, "b.md")["status"] == "failed"


def test_missing_collection_root_and_grants(kb_store, docs_root):
    (docs_root / "a.md").write_text("# T\n\ntext here", encoding="utf-8")
    d = deps_for(kb_store, kb_store.settings, docs_root)
    coll = kb_store.create_collection("main")
    job = kb_store.enqueue_job(coll["id"], "nowhere", "a.md")
    with pytest.raises(IngestError) as err:
        process_job(d, kb_store.claim_next_job(OWNER))
    assert err.value.code == "permission_denied"
    d.roots["docs"] = RootInfo(docs_root, frozenset())  # configured but not readable
    kb_store.enqueue_job(coll["id"], "docs", "a.md")
    with pytest.raises(IngestError) as err:
        process_job(d, kb_store.claim_next_job(OWNER))
    assert err.value.code == "permission_denied"
    kb_store.delete_collection(coll["id"])
    with pytest.raises(IngestError):  # the job rows cascade away with the collection; a stale dict is refused cleanly
        process_job(d, {**job, "collection_id": coll["id"]})


# ---------------------------------------------------------------------------------------------- paths in the worker


@pytest.mark.parametrize("rel", ["..\\outside.txt", "../outside.txt", "C:\\Windows\\win.ini", "\\\\srv\\share\\a.txt", "a.txt:stream",
                                 "CON.txt", "PROGRA~1\\a.txt", "sub\\..\\..\\x.txt", "dir/"])
def test_worker_revalidates_paths_whatever_the_queue_says(kb_store, docs_root, tmp_path, rel):
    (tmp_path / "outside.txt").write_text("secret outside the root")
    d = deps_for(kb_store, kb_store.settings, docs_root)
    with pytest.raises(IngestError) as err:
        run(d, rel)
    assert err.value.code in ("path_violation", "not_found", "invalid_input")
    assert kb_store.stats()["documents"] == {} and d.embedder.calls == []


def test_a_file_replaced_by_a_link_after_queueing_is_refused(kb_store, docs_root, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.txt").write_text("secret " * 300)
    (docs_root / "inner").mkdir()
    d = deps_for(kb_store, kb_store.settings, docs_root)
    coll = kb_store.create_collection("main")
    kb_store.enqueue_job(coll["id"], "docs", "inner/secret.txt")  # queued while inner/ was an ordinary folder ...
    (docs_root / "inner").rmdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(docs_root / "inner"), str(tmp_path / "elsewhere")], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(IngestError) as err:  # ... and became a junction to somewhere else before the worker ran
        process_job(d, kb_store.claim_next_job(OWNER))
    assert err.value.code == "path_violation" and "junction" in err.value.message
    assert d.embedder.calls == []


def test_symlinked_file_is_refused(kb_store, docs_root, tmp_path):
    (tmp_path / "t.txt").write_text("target " * 100)
    try:
        os.symlink(tmp_path / "t.txt", docs_root / "link.txt")
    except OSError as exc:
        pytest.skip(f"symlinks need Developer Mode or elevation here: {exc}")
    with pytest.raises(IngestError) as err:
        run(deps_for(kb_store, kb_store.settings, docs_root), "link.txt")
    assert err.value.code == "path_violation"


def test_a_file_that_changes_during_ingestion_is_not_trusted(kb_store, docs_root):
    path = docs_root / "a.md"
    path.write_text(topic_markdown("music", 6, 4), encoding="utf-8")

    class ChangesFile(HashEmbedder):
        def embed(self, texts):
            if len(self.calls) == 1:
                path.write_text(path.read_text() + "\n\nAn extra paragraph that was not there at the start.", encoding="utf-8")
            return super().embed(texts)

    d = deps_for(kb_store, kb_store.settings, docs_root, ChangesFile())
    err = refuse(d, "a.md", "file_changed")
    assert "changed while" in err.message and doc_of(kb_store, "a.md")["status"] == "failed" and kb_store.stats()["chunks"] == 0


# ---------------------------------------------------------------------------------------------- cancellation and outages


def test_cancel_stops_at_the_next_batch_and_removes_the_partial_document(kb_store, docs_root):
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    coll = kb_store.create_collection("main")
    job = kb_store.enqueue_job(coll["id"], "docs", "big.md")
    job = kb_store.claim_next_job(OWNER)

    class CancelsAfterFirstBatch(HashEmbedder):
        def embed(self, texts):
            out = super().embed(texts)
            if len(self.calls) == 1:
                kb_store.request_cancel(job["id"])  # the user presses cancel while the job is running
            return out

    with pytest.raises(Cancelled):
        process_job(deps_for(kb_store, kb_store.settings, docs_root, CancelsAfterFirstBatch()), job)
    doc = doc_of(kb_store, "big.md")
    assert doc["status"] == "cancelled" and doc["chunks_done"] == 0
    assert kb_store.stats()["chunks"] == 0 and kb_store.stats()["vectors"] == 0  # nothing half-indexed is left searchable


def test_embedding_outage_keeps_the_checkpoint_and_resumes_later(kb_store, docs_root):
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")

    class OutageAfterTwoBatches(HashEmbedder):
        down = False

        def embed(self, texts):
            if len(self.calls) >= 2 and self.down:
                self.calls.append(texts)  # counted as attempts
                raise ProviderUnavailable("Ollama is not reachable at test.")
            return super().embed(texts)

    emb = OutageAfterTwoBatches()
    emb.down = True
    d = deps_for(kb_store, kb_store.settings, docs_root, emb)
    err = refuse(d, "big.md", "embedding_unavailable")
    assert err.retryable and "resume" in err.message
    doc = doc_of(kb_store, "big.md")
    assert doc["status"] == "ingesting" and doc["chunks_done"] == 8  # the checkpoint survived the outage
    assert len(emb.calls) == 2 + 2  # the failing batch was tried twice (embed_retries = 1 in the test settings)
    emb.down = False  # Ollama is back; queueing the same file resumes it
    d.owner = OWNER
    _, result = run(d, "big.md")
    assert result["outcome"] == "ingested" and kb_store.stats()["chunks"] == result["chunks"] == kb_store.stats()["vectors"]


def test_missing_embedding_model_and_dimension_mismatch(kb_store, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")

    class NoModel(HashEmbedder):
        def embed(self, texts):
            raise ModelNotAvailable("Embedding model 'x' is not installed in Ollama. Run: ollama pull x")

    err = refuse(deps_for(kb_store, kb_store.settings, docs_root, NoModel()), "a.md", "embedding_model_missing")
    assert err.retryable and "ollama pull" in err.message
    assert doc_of(kb_store, "a.md")["status"] == "ingesting"  # kept: queue it again after pulling the model

    class WrongSize(HashEmbedder):
        def embed(self, texts):
            raise EmbeddingDimensionError("The embedding model returned 64-dimensional vectors but embedding.dimensions is 128.")

    err = refuse(deps_for(kb_store, kb_store.settings, docs_root, WrongSize()), "a.md", "embedding_dimension_mismatch")
    assert not err.retryable
    assert doc_of(kb_store, "a.md")["status"] == "failed" and kb_store.stats()["chunks"] == 0


def test_losing_the_lease_leaves_the_other_workers_document_alone(kb_store, docs_root):
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    coll = kb_store.create_collection("main")
    kb_store.enqueue_job(coll["id"], "docs", "big.md")
    job = kb_store.claim_next_job(OWNER)

    class LeaseStolen(HashEmbedder):
        def embed(self, texts):
            out = super().embed(texts)
            if len(self.calls) == 1:  # a stall-kill made the backend requeue the job and another worker claimed it
                kb_store.requeue_orphans(lambda owner: False, 3)
                kb_store.claim_next_job("other:1")
            return out

    with pytest.raises(LeaseLost):
        process_job(deps_for(kb_store, kb_store.settings, docs_root, LeaseStolen()), job)
    assert doc_of(kb_store, "big.md")["status"] == "ingesting"  # not failed, not purged: the new owner resumes it
    assert kb_store.stats()["chunks"] == doc_of(kb_store, "big.md")["chunks_done"] > 0


def test_unexpected_errors_fail_the_document_instead_of_leaving_a_trap(kb_store, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")

    class Bug(HashEmbedder):
        def embed(self, texts):
            raise ZeroDivisionError("bug in the embedder")

    with pytest.raises(ZeroDivisionError):
        run(deps_for(kb_store, kb_store.settings, docs_root, Bug()), "a.md")
    assert doc_of(kb_store, "a.md")["status"] == "failed" and kb_store.stats()["chunks"] == 0


# ---------------------------------------------------------------------------------------------- jobs


def test_claim_order_attempts_and_orphan_policy(kb_store):
    coll = kb_store.create_collection("main")
    ids = [kb_store.enqueue_job(coll["id"], "docs", f"{i}.txt")["id"] for i in range(3)]
    first = kb_store.claim_next_job("w:1")
    assert first["id"] == ids[0] and first["status"] == "running" and first["attempts"] == 1 and first["claimed_by"] == "w:1"
    assert kb_store.claim_next_job("w:1")["id"] == ids[1]
    # worker w:1 died with two jobs; the first has already used all its attempts
    kb_store.db.connection().__enter__().execute("UPDATE ingestion_jobs SET attempts = 3 WHERE id = ?", (ids[0],))
    outcome = kb_store.requeue_orphans(lambda owner: False, 3)
    assert outcome == {"requeued": [ids[1]], "failed": [ids[0]]}
    failed = kb_store.get_job(ids[0])
    assert failed["status"] == "failed" and failed["error_code"] == "worker_died" and "malformed" in failed["error"]
    alive = kb_store.requeue_orphans(lambda owner: True, 3)
    assert alive == {"requeued": [], "failed": []}


def test_cancelling_a_queued_job_is_immediate_and_a_finished_job_conflicts(kb_store):
    from xodus.storage.knowledge import Conflict

    coll = kb_store.create_collection("main")
    job = kb_store.enqueue_job(coll["id"], "docs", "a.txt")
    assert kb_store.request_cancel(job["id"])["status"] == "cancelled"
    with pytest.raises(Conflict):
        kb_store.request_cancel(job["id"])
    assert kb_store.claim_next_job(OWNER) is None


def test_cancelling_an_interrupted_job_while_it_is_queued_removes_its_partial_document(kb_store):
    """Gate F dry run: a job stopped by the resource governor (or a pause, or a worker crash) goes back to the queue with
    a partial document; cancelling it then must not leave that document 'ingesting' for ever."""
    from xodus.storage.knowledge import ChunkRecord

    coll = kb_store.create_collection("main")
    job = kb_store.enqueue_job(coll["id"], "docs", "a.txt")
    kb_store.claim_next_job(OWNER)
    doc = kb_store.start_document(collection_id=coll["id"], root="docs", path="a.txt", title="a.txt", fmt="text",
                                  size_bytes=10, content_hash="h", chunker_version=CHUNKER_VERSION, encoding="utf-8")
    kb_store.append_chunks(doc["id"], coll["id"], [ChunkRecord(0, "partial text", "t0", "H", None, None, 0, 12, 3)],
                           [[0.1] * DIM])
    assert kb_store.requeue_running() == [job["id"]]  # what a pause or a resource hold does
    other = kb_store.start_document(collection_id=coll["id"], root="docs", path="b.txt", title="b.txt", fmt="text",
                                    size_bytes=10, content_hash="h2", chunker_version=CHUNKER_VERSION, encoding="utf-8")
    assert kb_store.request_cancel(job["id"])["status"] == "cancelled"
    assert kb_store.get_document(doc["id"])["status"] == "cancelled" and kb_store.count_chunks() == 0
    assert kb_store.get_document(other["id"])["status"] == "ingesting"  # another file's document is not touched


def test_finish_job_requires_the_lease(kb_store):
    coll = kb_store.create_collection("main")
    kb_store.enqueue_job(coll["id"], "docs", "a.txt")
    job = kb_store.claim_next_job("w:1")
    with pytest.raises(LeaseLost):
        kb_store.finish_job(job["id"], "w:2", "completed")
    with pytest.raises(LeaseLost):
        kb_store.heartbeat_job(job["id"], "w:2")
    kb_store.finish_job(job["id"], "w:1", "completed", outcome="ingested")
    assert kb_store.get_job(job["id"])["status"] == "completed"


def test_chunker_version_change_triggers_reingestion(kb_store, docs_root, monkeypatch):
    (docs_root / "a.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    d = deps_for(kb_store, kb_store.settings, docs_root)
    run(d, "a.md")
    import xodus.ingestion.pipeline as pipeline

    monkeypatch.setattr(pipeline, "CHUNKER_VERSION", CHUNKER_VERSION + 1)
    _, again = run(d, "a.md")
    assert again["outcome"] == "replaced"  # an old chunker's output is never silently kept
    assert DIM == 128
