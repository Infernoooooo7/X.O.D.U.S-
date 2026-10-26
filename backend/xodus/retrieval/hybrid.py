"""Hybrid retrieval: nearest-neighbour vectors and BM25 keywords, fused by Reciprocal Rank Fusion.

  query -> embed (instruction template) -> top k_vector by cosine distance   \
        -> safe FTS5 query            -> top k_fts by BM25                  /-> RRF -> top k -> evidence budget
Both searches are restricted to the caller's collections and to documents that are fully ingested. If the
embedder is unavailable the search degrades to keywords only and says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xodus.config.settings import EmbeddingSettings, KnowledgeSettings
from xodus.providers.base import ProviderError
from xodus.retrieval.fts import build_match, count_matching_terms, query_terms
from xodus.storage.knowledge import KnowledgeStore

EmbedQuery = Callable[[str], Awaitable[list[float]]]


class QueryError(ValueError):
    pass


@dataclass
class Evidence:
    chunk_id: int
    document_id: int
    document: str
    collection: str
    text: str
    heading_path: str
    locator: str | None
    page: int | None
    score: float
    vector_rank: int | None = None
    fts_rank: int | None = None
    vector_similarity: float | None = None
    term_matches: int = 0
    token_estimate: int = 0

    def summary(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "document": self.document, "collection": self.collection,
                "heading": self.heading_path, "locator": self.locator, "page": self.page, "score": round(self.score, 5),
                "vector_rank": self.vector_rank, "fts_rank": self.fts_rank,
                "vector_similarity": None if self.vector_similarity is None else round(self.vector_similarity, 4),
                "term_matches": self.term_matches, "tokens": self.token_estimate}


@dataclass
class SearchResult:
    evidence: list[Evidence]
    sufficient: bool
    mode: str  # hybrid | keywords_only | vector_only | none
    terms: list[str]
    degraded: str | None = None
    reasons: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """score(id) = sum over rankings of 1 / (k + rank), rank starting at 1."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


class Retriever:
    def __init__(self, store: KnowledgeStore, knowledge: KnowledgeSettings, embedding: EmbeddingSettings,
                 embed_query: EmbedQuery | None) -> None:
        self.store = store
        self.k = knowledge
        self.embedding = embedding
        self._embed_query = embed_query

    async def search(self, query: str, collection_ids: list[int], *, k: int | None = None, origin: str = "user",
                     request_id: str | None = None, token_budget: int | None = None) -> SearchResult:
        query = query.strip()
        if not query:
            raise QueryError("The query is empty.")
        if len(query) > self.k.max_query_chars:
            raise QueryError(f"The query is longer than {self.k.max_query_chars} characters.")
        limit = max(1, min(k or self.k.default_k, self.k.max_k))
        budget = token_budget if token_budget is not None else self.k.evidence_token_budget
        t_start = time.perf_counter()
        terms = query_terms(query)
        match = build_match(terms)
        embed_ms = vector_ms = fts_ms = None
        degraded: str | None = None
        vector_hits: list[tuple[int, float]] = []

        if self._embed_query is None:
            degraded = "No embedder is configured; using keyword search only."
        else:
            t = time.perf_counter()
            try:
                vector = await self._embed_query(self.embedding.query_template.replace("{text}", query))
                embed_ms = (time.perf_counter() - t) * 1000
                t = time.perf_counter()
                vector_hits = await asyncio.to_thread(self.store.vector_candidates, vector, collection_ids, self.k.k_vector)
                vector_ms = (time.perf_counter() - t) * 1000
            except ProviderError as exc:
                degraded = f"Vector search is unavailable ({exc.message}); using keyword search only."

        t = time.perf_counter()
        fts_hits = await asyncio.to_thread(self.store.fts_candidates, match, collection_ids, self.k.k_fts)
        fts_ms = (time.perf_counter() - t) * 1000

        vector_rank = {cid: i for i, (cid, _) in enumerate(vector_hits, start=1)}
        similarity = {cid: 1.0 - dist for cid, dist in vector_hits}  # cosine distance = 1 - cosine similarity
        fts_rank = {cid: i for i, (cid, _) in enumerate(fts_hits, start=1)}
        fused = rrf_fuse([[c for c, _ in vector_hits], [c for c, _ in fts_hits]], self.k.rrf_k)
        ordered = sorted(fused, key=lambda cid: (-fused[cid], cid))[:limit]
        chunks = await asyncio.to_thread(self.store.get_chunks, ordered)

        evidence: list[Evidence] = []
        used = 0
        for cid in ordered:
            row = chunks.get(cid)
            if row is None:  # removed or not ready any more
                continue
            tokens = max(1, math.ceil(len(row["text"]) / self.k.chars_per_token))
            if evidence and used + tokens > budget:
                break
            evidence.append(Evidence(
                chunk_id=cid, document_id=row["document_id"], document=row["document"], collection=row["collection"],
                text=row["text"], heading_path=row["heading_path"], locator=row["locator"], page=row["page"],
                score=fused[cid], vector_rank=vector_rank.get(cid), fts_rank=fts_rank.get(cid),
                vector_similarity=similarity.get(cid), term_matches=count_matching_terms(row["text"], terms),
                token_estimate=tokens))
            used += tokens

        mode = ("hybrid" if vector_hits and fts_hits else "vector_only" if vector_hits else
                "keywords_only" if fts_hits else "none")
        sufficient, reasons = self._sufficient(evidence, terms, vector_available=bool(vector_hits) or degraded is None)
        total_ms = (time.perf_counter() - t_start) * 1000
        if origin != "none":
            await asyncio.to_thread(
                self.store.log_retrieval, request_id=request_id, origin=origin,
                query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(), query_chars=len(query),
                collections=",".join(map(str, collection_ids)), mode=mode, n_vector=len(vector_hits), n_fts=len(fts_hits),
                chunk_ids=[e.chunk_id for e in evidence], sufficient=sufficient, embed_ms=embed_ms, vector_ms=vector_ms,
                fts_ms=fts_ms, total_ms=total_ms)
        return SearchResult(evidence, sufficient, mode, terms, degraded, reasons,
                            {"n_vector": len(vector_hits), "n_fts": len(fts_hits), "evidence_tokens": used,
                             "embed_ms": embed_ms, "vector_ms": vector_ms, "fts_ms": fts_ms, "total_ms": round(total_ms, 1)})

    def _sufficient(self, evidence: list[Evidence], terms: list[str], vector_available: bool) -> tuple[bool, list[str]]:
        """Provisional test for 'is there evidence at all'. Not a relevance guarantee (calibration: Gate D report)."""
        if not evidence:
            return False, ["no matching passages"]
        sims = [e.vector_similarity for e in evidence if e.vector_similarity is not None]
        if self.k.min_vector_similarity > 0 and sims:
            best = max(sims)
            ok = best >= self.k.min_vector_similarity
            return ok, [] if ok else [f"best vector similarity {best:.2f} is below {self.k.min_vector_similarity:.2f}"]
        if terms and self.k.min_term_match_ratio > 0:
            best_ratio = max(e.term_matches for e in evidence) / len(terms)
            ok = best_ratio >= self.k.min_term_match_ratio
            return ok, [] if ok else [f"best passage matches {best_ratio:.0%} of the query terms (needs {self.k.min_term_match_ratio:.0%})"]
        return True, []
