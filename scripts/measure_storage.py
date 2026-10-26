"""Measure the SQLite storage adapter up to the supported corpus limit (100,000 chunks of 1024-dim vectors).

Run from backend/:   uv run python ..\\scripts\\measure_storage.py [--chunks 100000]

Synthetic data, real code paths: chunks are written with KnowledgeStore.append_chunks (the same call the ingestion
worker makes: rows + FTS entries + vectors + checkpoint in one transaction) and searched with the production
Retriever. Chunk text is ~1,800 characters of Zipf-distributed pseudo-words; vectors are random 1024-dim floats
(brute-force search cost does not depend on their values; recall is NOT measured here).

At each checkpoint it records file sizes, bytes per chunk, vector / keyword / hybrid search latency (p50, p95, max over
30 queries), this process's memory (USS and peak working set), and insert throughput. At the end it checks that the
chunk limit is enforced and times the deletion of one document. Writes experiments/results/gate-d/scale.json.

What it does NOT measure: embedding time (Ollama), PostgreSQL (not installed), or multi-user concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

import psutil

from xodus.config.settings import KnowledgeSettings
from xodus.config.settings import EmbeddingSettings
from xodus.retrieval.hybrid import Retriever
from xodus.storage.knowledge import ChunkRecord, CorpusLimitExceeded, KnowledgeStore
from xodus.storage.sqlite import Database

DIM = 1024
CHUNKS_PER_DOC = 100
BATCH = 16
MiB = 2**20
OUT = Path(__file__).resolve().parents[1] / "experiments" / "results" / "gate-d"


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


class Generator:
    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.words = [f"w{n:05d}" for n in range(20_000)]
        self.weights = [1.0 / (rank + 1) for rank in range(len(self.words))]  # Zipf
        self.pool = [unit([self.rng.gauss(0, 1) for _ in range(DIM)]) for _ in range(2000)]
        self.counter = 0

    def text(self) -> str:
        return " ".join(self.rng.choices(self.words, self.weights, k=300))

    def vector(self) -> list[float]:
        self.counter += 1
        base = list(self.pool[self.counter % len(self.pool)])
        base[self.counter % DIM] += 1e-3 * ((self.counter // DIM) % 997)  # make every vector distinct
        return base

    def chunk(self, ordinal: int) -> tuple[ChunkRecord, list[float]]:
        text = self.text()
        return ChunkRecord(ordinal, text, hashlib.sha256(text.encode()).hexdigest(), f"Section {ordinal // 10}", f"row {ordinal}", None,
                           0, len(text), len(text) // 4), self.vector()


def process_memory() -> dict[str, float]:
    proc = psutil.Process()
    info = proc.memory_full_info()
    return {"uss_mib": round(info.uss / MiB, 1), "working_set_mib": round(info.rss / MiB, 1), "peak_working_set_mib": round(info.peak_wset / MiB, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=100_000)
    ap.add_argument("--checkpoints", default="1000,10000,25000,50000,100000")
    ap.add_argument("--queries", type=int, default=30)
    args = ap.parse_args()
    checkpoints = [c for c in (int(x) for x in args.checkpoints.split(",")) if c <= args.chunks]
    if args.chunks not in checkpoints:
        checkpoints.append(args.chunks)

    tmp = Path(tempfile.mkdtemp(prefix="xodus-scale-"))
    db = Database(tmp / "scale.db", embedding_dim=DIM)
    db.open()
    store = KnowledgeStore(db, max_chunks=100_000)
    gen = Generator()
    k = KnowledgeSettings()
    collection = store.create_collection("scale")
    query_vectors = [gen.pool[(i * 37) % len(gen.pool)] for i in range(args.queries)]

    async def embed_query(_text: str) -> list[float]:
        return query_vectors[embed_query.n % len(query_vectors)]

    embed_query.n = 0  # type: ignore[attr-defined]
    retriever = Retriever(store, k, EmbeddingSettings(), embed_query)
    query_words = [" ".join(gen.rng.choices(gen.words[:2000], k=3)) for _ in range(args.queries)]
    results: dict[str, object] = {"dim": DIM, "chunks_per_document": CHUNKS_PER_DOC, "batch": BATCH, "python": sys.version.split()[0],
                                  "page_size": None, "checkpoints": []}
    start = time.perf_counter()
    inserted, doc_index = 0, 0
    insert_times: list[float] = []

    def measure(n: int) -> dict[str, object]:
        store.checkpoint()
        stats = store.stats()
        vec_ms, fts_ms, hyb_ms = [], [], []
        cid = [collection["id"]]
        for i, qv in enumerate(query_vectors):
            t = time.perf_counter()
            store.vector_candidates(qv, cid, k.k_vector)
            vec_ms.append((time.perf_counter() - t) * 1000)
        from xodus.retrieval.fts import build_match, query_terms

        for words in query_words:
            t = time.perf_counter()
            store.fts_candidates(build_match(query_terms(words)), cid, k.k_fts)
            fts_ms.append((time.perf_counter() - t) * 1000)
        for i, words in enumerate(query_words):
            embed_query.n = i
            t = time.perf_counter()
            asyncio.run(retriever.search(words, cid, k=8, origin="none"))
            hyb_ms.append((time.perf_counter() - t) * 1000)
        lat = lambda xs: {"p50_ms": round(statistics.median(xs), 1), "p95_ms": round(pct(xs, 0.95), 1), "max_ms": round(max(xs), 1)}  # noqa: E731
        return {"chunks": n, "file_bytes": stats["file_bytes"], "db_mib": round(stats["file_bytes"]["db"] / MiB, 1),
                "bytes_per_chunk": stats["database_bytes_per_chunk"], "vector_search": lat(vec_ms), "keyword_search": lat(fts_ms),
                "hybrid_search_without_embedding": lat(hyb_ms), "first_vector_query_ms": round(vec_ms[0], 1),
                "memory": process_memory(), "elapsed_s": round(time.perf_counter() - start, 1),
                "insert_ms_per_batch_recent_median": round(statistics.median(insert_times[-200:]) * 1000, 1) if insert_times else None,
                "free_pages": stats["free_pages"]}

    pending = list(checkpoints)
    while inserted < args.chunks:
        doc = store.start_document(collection_id=collection["id"], root="scale", path=f"d{doc_index}", title=f"doc {doc_index}", fmt="text",
                                   size_bytes=1, content_hash=str(doc_index), chunker_version=1, encoding="utf-8")
        for first in range(0, CHUNKS_PER_DOC, BATCH):
            records, vectors = [], []
            for ordinal in range(first, min(first + BATCH, CHUNKS_PER_DOC)):
                if inserted + len(records) >= args.chunks:
                    break
                rec, vec = gen.chunk(ordinal)
                records.append(rec)
                vectors.append(vec)
            if not records:
                break
            t = time.perf_counter()
            store.append_chunks(doc["id"], collection["id"], records, vectors)
            insert_times.append(time.perf_counter() - t)
            inserted += len(records)
        store.finish_document(doc["id"])
        doc_index += 1
        if pending and inserted >= pending[0]:
            n = pending.pop(0)
            row = measure(inserted)
            results["checkpoints"].append(row)  # type: ignore[union-attr]
            print(json.dumps(row), flush=True)

    # the envelope limit is enforced, and deleting one document at full size is timed
    extra = store.start_document(collection_id=collection["id"], root="scale", path="over", title="over", fmt="text", size_bytes=1,
                                content_hash="x", chunker_version=1, encoding="utf-8")
    rec, vec = gen.chunk(0)
    try:
        store.append_chunks(extra["id"], collection["id"], [rec], [vec])
        results["limit_enforced"] = False
    except CorpusLimitExceeded as exc:
        results["limit_enforced"] = True
        results["limit_message"] = exc.message
    store.fail_document(extra["id"], "over the limit")
    victim = store.find_document(collection["id"], "scale", "d7")
    t = time.perf_counter()
    report = store.delete_document(victim["id"])
    results["delete_one_document_at_full_size"] = {"chunks": report["chunks"], "seconds": round(time.perf_counter() - t, 3)}
    results["after_delete_verify_clean"] = store.verify_gone(victim["id"], report["chunk_ids"], [])["clean"]
    results["total_seconds"] = round(time.perf_counter() - start, 1)
    results["insert_rate_chunks_per_s"] = round(args.chunks / sum(insert_times), 1)
    final = store.stats()
    results["final"] = {"chunks": final["chunks"], "vectors": final["vectors"], "file_bytes": final["file_bytes"], "pages": final["pages"],
                        "page_size": final["page_size"]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scale.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    db.close()
    print("\nwrote", OUT / "scale.json")
    for name in ("scale.db", "scale.db-wal", "scale.db-shm"):
        (tmp / name).unlink(missing_ok=True)
    tmp.rmdir()
    return 0


if __name__ == "__main__":
    sys.exit(main())
