"""Hybrid retrieval: fusion, safe FTS queries, collection and permission filters, degraded mode, exactness."""

from __future__ import annotations

import math
import random

import pytest

from tests.kb_helpers import DIM, HashEmbedder, TOPICS, hash_vector, topic_markdown
from xodus.config.settings import EmbeddingSettings, KnowledgeSettings
from xodus.ingestion.pipeline import PipelineDeps, Progress, RootInfo, process_job
from xodus.providers.base import ProviderUnavailable
from xodus.retrieval.fts import MAX_TERMS, build_match, count_matching_terms, query_terms
from xodus.retrieval.hybrid import QueryError, Retriever, rrf_fuse


# ---------------------------------------------------------------------------------------------- pure functions


def test_rrf_fusion_matches_the_formula_exactly():
    scores = rrf_fuse([[1, 2, 3], [2, 1, 4]], k=60)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62) and scores[2] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[3] == pytest.approx(1 / 63) and scores[4] == pytest.approx(1 / 63)
    assert max(scores, key=scores.get) in (1, 2) and scores[1] > scores[3]  # appearing in both lists beats appearing in one
    assert rrf_fuse([], 60) == {} and rrf_fuse([[5]], 10) == {5: pytest.approx(1 / 11)}


def test_query_terms_drop_stop_words_dedupe_and_cap():
    assert query_terms("How do I parse JSON in Python?") == ["parse", "json", "python"]
    assert query_terms("Telescope telescope TELESCOPE nebula") == ["telescope", "nebula"]
    assert query_terms("to be or not to be") == ["to", "be", "or", "not"]  # only stop words: keep them rather than nothing
    assert query_terms("route 66 and a b") == ["route", "66"]
    assert len(query_terms(" ".join(f"word{i}" for i in range(50)))) == MAX_TERMS
    assert query_terms("Größe Café 東京") == ["größe", "café", "東京"]
    assert query_terms("!!! ??? ...") == []


@pytest.mark.parametrize("hostile", [
    'foo" OR "bar', "NEAR(a b)", "*", "-term", "text:secret", "heading_path:Astronomy", "a AND NOT b", '"', "(", ")", "^x", "a:b:c",
    "{x}", "'; DROP TABLE chunks; --", "\"\"\"\"", "OR", "AND", "NOT", "col:*", "a OR b OR c OR d", "\x00zero", "tab\tnewline\n",
])
def test_hostile_queries_become_plain_quoted_words(kb_store, hostile):
    terms = query_terms(hostile)
    match = build_match(terms)
    assert all(t.replace("_", "a").isalnum() for t in terms)
    assert set(match.replace(" OR ", " ").split()) <= {f'"{t}"' for t in terms}  # nothing but quoted word tokens
    coll = kb_store.create_collection("c")
    kb_store.fts_candidates(match, [coll["id"]], 5)  # must never raise an FTS5 syntax error


def test_count_matching_terms_uses_whole_words():
    assert count_matching_terms("The telescope sees nebulae", ["telescope", "nebula", "see"]) == 1


# ---------------------------------------------------------------------------------------------- an indexed corpus


@pytest.fixture
def corpus(kb_store, docs_root):
    """Six single-topic documents in 'main' and two more in 'other', indexed with the hashing embedder."""
    deps = PipelineDeps(kb_store.settings, kb_store, HashEmbedder(), {"docs": RootInfo(docs_root, frozenset({"fs.read"}))}, "t:1", Progress(),
                        lambda s: None)
    ids = {}
    for collection, topics in (("main", list(TOPICS)), ("other", ["astronomy", "music"])):
        coll = kb_store.create_collection(collection)
        ids[collection] = coll["id"]
        for topic in topics:
            rel = f"{collection}-{topic}.md"
            (docs_root / rel).write_text(topic_markdown(topic, 3, 3), encoding="utf-8")
            kb_store.enqueue_job(coll["id"], "docs", rel)
            process_job(deps, kb_store.claim_next_job("t:1"))
    return ids


def make_retriever(store, **knowledge):
    settings = store.settings
    k = settings.knowledge.model_copy(update=knowledge)

    async def embed(text: str):
        return hash_vector(text, DIM)

    return Retriever(store, k, settings.embedding, embed)


QUERIES = {
    "astronomy": "telescope nebula redshift supernova pulsar",
    "cooking": "whisk eggs sugar sponge cake sourdough dough",
    "networking": "router packets TCP handshake DNS firewall",
    "gardening": "tomato seedlings mulch roses compost soil",
    "music": "orchestra oboe violins fugue symphony conductor",
    "finance": "compound interest portfolio bonds inflation mortgage",
}


@pytest.mark.parametrize("topic", list(QUERIES))
async def test_each_topic_query_finds_its_own_document(kb_store, corpus, topic):
    r = make_retriever(kb_store)
    result = await r.search(QUERIES[topic], [corpus["main"]], k=5)
    assert result.mode == "hybrid" and result.sufficient
    own = kb_store.find_document(corpus["main"], "docs", f"main-{topic}.md")
    own_chunks = len(kb_store.list_chunks(own["id"], 200))
    assert own_chunks >= 2
    assert result.evidence[0].document == f"main-{topic}.md"
    assert sum(e.document == f"main-{topic}.md" for e in result.evidence) == own_chunks  # every chunk of the right document ranks first


async def test_results_carry_both_rankings_and_cite_able_ids(kb_store, corpus):
    r = make_retriever(kb_store)
    result = await r.search("telescope nebula", [corpus["main"]], k=3)
    top = result.evidence[0]
    assert top.vector_rank and top.fts_rank and top.vector_similarity is not None and top.term_matches == 2
    assert top.score == pytest.approx(1 / (60 + top.vector_rank) + 1 / (60 + top.fts_rank))
    assert [e.score for e in result.evidence] == sorted((e.score for e in result.evidence), reverse=True)
    assert all(isinstance(e.chunk_id, int) and e.collection == "main" for e in result.evidence)


async def test_a_chunk_found_by_only_one_signal_still_appears(kb_store, corpus):
    """The vector signal is steered at a gardening chunk, the keywords at an astronomy chunk: both are returned."""
    garden_chunk = kb_store.list_chunks(kb_store.find_document(corpus["main"], "docs", "main-gardening.md")["id"], 5)[0]
    garden_vec = hash_vector(garden_chunk["text"], DIM)

    async def steered(_text: str):
        return garden_vec

    # k_vector=2: the vector search returns only the two nearest chunks, so the keyword hits are not also vector hits
    narrow = kb_store.settings.knowledge.model_copy(update={"k_vector": 2})
    r = Retriever(kb_store, narrow, kb_store.settings.embedding, steered)
    result = await r.search("telescope nebula redshift", [corpus["main"]], k=10)
    docs = {e.document for e in result.evidence}
    assert "main-gardening.md" in docs and "main-astronomy.md" in docs
    from_vector_only = [e for e in result.evidence if e.vector_rank and not e.fts_rank]
    from_fts_only = [e for e in result.evidence if e.fts_rank and not e.vector_rank]
    assert from_vector_only and from_fts_only


async def test_collection_filter_applies_to_both_searches(kb_store, corpus):
    r = make_retriever(kb_store)
    only_other = await r.search(QUERIES["astronomy"], [corpus["other"]], k=10)
    assert {e.collection for e in only_other.evidence} == {"other"}
    only_main = await r.search(QUERIES["astronomy"], [corpus["main"]], k=10)
    assert {e.collection for e in only_main.evidence} == {"main"}
    both = await r.search(QUERIES["astronomy"], [corpus["main"], corpus["other"]], k=10)
    assert {e.collection for e in both.evidence} == {"main", "other"}


async def test_a_collection_you_may_not_read_never_leaks(kb_store, corpus):
    """Finance exists only in 'main'. Searching 'other' for it must return nothing that came from 'main'."""
    r = make_retriever(kb_store)
    result = await r.search(QUERIES["finance"], [corpus["other"]], k=20)
    assert all(e.collection == "other" and "finance" not in e.document for e in result.evidence)
    assert all("interest" not in e.text.lower() or e.collection == "other" for e in result.evidence)
    nothing = await r.search(QUERIES["finance"], [], k=5)
    assert nothing.evidence == [] and not nothing.sufficient and nothing.mode == "none"


async def test_documents_still_being_ingested_are_invisible(kb_store, corpus):
    from tests.test_migrations import _rec

    coll = kb_store.get_collection(corpus["main"])
    doc = kb_store.start_document(collection_id=coll["id"], root="docs", path="partial.md", title="partial.md", fmt="markdown", size_bytes=1,
                                  content_hash="h", chunker_version=1, encoding="utf-8")
    text = "zebrafish quokka axolotl unique partial words"
    kb_store.append_chunks(doc["id"], coll["id"], [_rec(0, text)], [hash_vector(text, DIM)])
    r = make_retriever(kb_store)
    hidden = await r.search("zebrafish quokka axolotl", [coll["id"]], k=5)
    assert all(e.document != "partial.md" for e in hidden.evidence)
    kb_store.finish_document(doc["id"])
    shown = await r.search("zebrafish quokka axolotl", [coll["id"]], k=5)
    assert shown.evidence[0].document == "partial.md"


async def test_evidence_is_trimmed_to_the_token_budget_but_never_empty(kb_store, corpus):
    r = make_retriever(kb_store)
    wide = await r.search(QUERIES["music"], [corpus["main"]], k=10, token_budget=100_000)
    tight = await r.search(QUERIES["music"], [corpus["main"]], k=10, token_budget=900)
    assert len(tight.evidence) < len(wide.evidence) and sum(e.token_estimate for e in tight.evidence) <= 900 + wide.evidence[0].token_estimate
    tiny = await r.search(QUERIES["music"], [corpus["main"]], k=10, token_budget=1)
    assert len(tiny.evidence) == 1  # the best passage is always returned
    assert tight.stats["evidence_tokens"] == sum(e.token_estimate for e in tight.evidence)


async def test_k_is_clamped_and_results_are_deterministic(kb_store, corpus):
    r = make_retriever(kb_store, max_k=5, default_k=3)
    assert len((await r.search(QUERIES["music"], [corpus["main"]], k=999, token_budget=10**6)).evidence) <= 5
    assert len((await r.search(QUERIES["music"], [corpus["main"]], token_budget=10**6)).evidence) == 3
    a = await r.search(QUERIES["finance"], [corpus["main"]], k=5)
    b = await r.search(QUERIES["finance"], [corpus["main"]], k=5)
    assert [e.chunk_id for e in a.evidence] == [e.chunk_id for e in b.evidence]


async def test_degrades_to_keywords_when_the_embedder_is_down(kb_store, corpus):
    async def down(_text: str):
        raise ProviderUnavailable("Ollama is not reachable.")

    r = Retriever(kb_store, kb_store.settings.knowledge, kb_store.settings.embedding, down)
    result = await r.search(QUERIES["networking"], [corpus["main"]], k=5)
    assert result.mode == "keywords_only" and "Vector search is unavailable" in result.degraded
    assert result.evidence[0].document == "main-networking.md" and all(e.vector_rank is None for e in result.evidence)
    no_embedder = Retriever(kb_store, kb_store.settings.knowledge, kb_store.settings.embedding, None)
    assert (await no_embedder.search(QUERIES["networking"], [corpus["main"]])).degraded.startswith("No embedder")


async def test_query_validation(kb_store, corpus):
    r = make_retriever(kb_store)
    with pytest.raises(QueryError, match="empty"):
        await r.search("   ", [corpus["main"]])
    with pytest.raises(QueryError, match="longer than 500"):
        await r.search("x" * 501, [corpus["main"]])


async def test_insufficient_evidence_for_an_unrelated_question(kb_store, corpus):
    r = make_retriever(kb_store)
    unrelated = await r.search("quantum chromodynamics lagrangian gauge symmetry", [corpus["main"]], k=5)
    assert not unrelated.sufficient and "query terms" in unrelated.reasons[0]
    related = await r.search("How does the TCP handshake work?", [corpus["main"]], k=5)
    assert related.sufficient
    strict = make_retriever(kb_store, min_vector_similarity=0.99)  # a vector threshold, when set, takes over
    assert not (await strict.search(QUERIES["music"], [corpus["main"]])).sufficient


async def test_hostile_queries_never_break_a_search(kb_store, corpus):
    r = make_retriever(kb_store)
    for hostile in ('foo" OR "bar', "NEAR(a b)", "*", "heading_path:astronomy", "'; DROP TABLE chunks;--", ")(*^:", "AND OR NOT"):
        result = await r.search(hostile, [corpus["main"]], k=3)
        assert result.mode in ("hybrid", "vector_only", "keywords_only", "none")
    assert kb_store.stats()["chunks"] > 0  # the table is still there


async def test_every_search_is_logged_without_the_query_text(kb_store, corpus):
    secret = "xylophonequery-7731-confidential"
    r = make_retriever(kb_store)
    await r.search(f"telescope {secret}", [corpus["main"]], k=3, origin="user", request_id="req123")
    with kb_store.db.connection() as conn:
        row = dict(conn.execute("SELECT * FROM retrieval_log ORDER BY id DESC LIMIT 1").fetchone())
    assert row["request_id"] == "req123" and row["origin"] == "user" and row["mode"] == "hybrid" and row["n_returned"] == 3
    assert row["query_chars"] == len(f"telescope {secret}") and len(row["query_hash"]) == 64 and row["total_ms"] > 0
    kb_store.checkpoint()
    raw = b"".join(p.read_bytes() for p in (kb_store.db.path, kb_store.db.path.with_name(kb_store.db.path.name + "-wal")) if p.exists())
    assert secret.encode() not in raw  # only the hash was kept


async def test_retrieval_log_is_bounded(kb_store, corpus):
    r = make_retriever(kb_store)
    for _ in range(3):
        await r.search("telescope", [corpus["main"]], k=1)
    for i in range(1005):
        kb_store.log_retrieval(request_id=None, origin="t", query_hash="h", query_chars=1, collections="1", mode="hybrid", n_vector=0,
                               n_fts=0, chunk_ids=[], sufficient=False, embed_ms=None, vector_ms=None, fts_ms=None, total_ms=1.0)
    with kb_store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM retrieval_log").fetchone()[0] <= 1001


# ---------------------------------------------------------------------------------------------- exactness


def test_vector_search_is_exact_against_brute_force(kb_store):
    from tests.test_migrations import _rec

    rng = random.Random(5)
    coll = kb_store.create_collection("exact")
    doc = kb_store.start_document(collection_id=coll["id"], root="r", path="v", title="v", fmt="text", size_bytes=1, content_hash="h",
                                  chunker_version=1, encoding="utf-8")
    vectors = []
    for start in range(0, 300, 20):
        batch = [[rng.gauss(0, 1) for _ in range(DIM)] for _ in range(20)]
        vectors += batch
        kb_store.append_chunks(doc["id"], coll["id"], [_rec(start + i, f"c{start + i}") for i in range(20)], batch)
    kb_store.finish_document(doc["id"])
    ids = [c["id"] for c in kb_store.list_chunks(doc["id"], 200, 0)] + [c["id"] for c in kb_store.list_chunks(doc["id"], 200, 200)]
    query = [rng.gauss(0, 1) for _ in range(DIM)]

    def cosine_distance(a, b):
        return 1 - sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    expected = sorted(range(300), key=lambda i: cosine_distance(query, vectors[i]))[:10]
    got = kb_store.vector_candidates(query, [coll["id"]], 10)
    assert [cid for cid, _ in got] == [ids[i] for i in expected]
    assert got[0][1] == pytest.approx(cosine_distance(query, vectors[expected[0]]), abs=1e-5)
    assert EmbeddingSettings().batch_size == 16 and KnowledgeSettings().rrf_k == 60
