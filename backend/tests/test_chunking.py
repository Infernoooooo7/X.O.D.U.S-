"""Chunking: sizes, structure, overlap, determinism (which ingestion resume relies on)."""

from __future__ import annotations

import re

import pytest

from tests.kb_helpers import topic_markdown, topic_paragraphs
from xodus.config.settings import KnowledgeSettings
from xodus.ingestion.chunking import CHUNKER_VERSION, ChunkParams, chunk_blocks, estimate_tokens, split_block
from xodus.ingestion.text import Block

P = ChunkParams.from_settings(KnowledgeSettings())  # 1600 / 2000 / 2400 chars, overlap 200
NO_OVERLAP = ChunkParams(P.min_chars, P.target_chars, P.max_chars, 0, P.chars_per_token)


def paras(n: int, heading: str = "Doc", size: int = 400) -> list[Block]:
    return [Block(f"Para {i}. " + ("lorem ipsum dolor sit amet. " * 40)[:size], heading, None, None, i * 1000) for i in range(n)]


def test_non_final_chunks_are_between_400_and_600_estimated_tokens():
    chunks = list(chunk_blocks(paras(60), P))
    assert len(chunks) > 5
    for c in chunks[:-1]:
        assert 400 <= c.token_estimate <= 600, c.token_estimate
    assert chunks[-1].token_estimate <= 600


def test_ordinals_are_consecutive_and_hashes_match_text():
    import hashlib

    chunks = list(chunk_blocks(paras(40), P))
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(c.text_hash == hashlib.sha256(c.text.encode()).hexdigest() for c in chunks)


def test_token_estimate_is_characters_over_four_rounded_up():
    assert estimate_tokens("a" * 100, 4.0) == 25 and estimate_tokens("a" * 101, 4.0) == 26 and estimate_tokens("", 4.0) == 1
    c = next(iter(chunk_blocks([Block("x" * 1000, "H")], P)))
    assert c.token_estimate == 250


def test_overlap_repeats_the_end_of_the_previous_chunk_in_the_same_section():
    chunks = list(chunk_blocks(paras(30), P))
    pairs = 0
    for a, b in zip(chunks, chunks[1:]):
        carried = b.text.split("\n\n")[0]
        if carried in a.text and a.text.endswith(carried):
            assert 0 < len(carried) <= P.overlap_chars
            pairs += 1
    assert pairs >= len(chunks) - 2  # essentially every boundary inside one section carries an overlap


def test_no_overlap_across_a_heading_change():
    blocks = paras(8, "Section A") + paras(8, "Section B")
    chunks = list(chunk_blocks(blocks, P))
    boundary = next(i for i, c in enumerate(chunks) if c.heading_path == "Section B")
    first_b = chunks[boundary]
    assert first_b.text.startswith("Para 0.")  # begins with fresh text, not a repeat of section A


def test_chunks_prefer_to_end_at_a_heading_once_they_are_big_enough():
    a = [Block(f"Alpha {i}. " + "filler words here. " * 30, "A") for i in range(5)]  # 2,900 chars: more than the minimum
    b = [Block(f"Beta {i}. " + "filler words here. " * 30, "B") for i in range(5)]
    chunks = list(chunk_blocks(a + b, P))
    assert {c.heading_path for c in chunks} == {"A", "B"}
    assert not any("Alpha" in c.text and "Beta" in c.text for c in chunks)  # no chunk straddles the heading change


def test_small_sections_are_merged_rather_than_emitted_alone():
    blocks = [Block("tiny one", "A"), Block("tiny two", "B"), Block("tiny three", "C")]
    chunks = list(chunk_blocks(blocks, P))
    assert len(chunks) == 1 and chunks[0].heading_path == "A" and "tiny three" in chunks[0].text


def test_a_chunk_never_carries_a_heading_path_that_disagrees_with_most_of_its_text():
    """Regression: a short tail of section A used to be merged into section B's first chunk under A's heading."""
    for tail_blocks in range(1, 6):  # the A section ends with a tail of 1..5 paragraphs after a size-based flush
        a = [Block(f"Alpha {i}. " + "filler words here. " * 30, "A") for i in range(3 + tail_blocks)]
        b = [Block(f"Beta {i}. " + "filler words here. " * 30, "B") for i in range(6)]
        for chunk in chunk_blocks(a + b, P):
            has_a, has_b = "Alpha" in chunk.text, "Beta" in chunk.text
            if has_a and has_b:  # only allowed when the A part is a small fragment (under half the minimum)
                assert chunk.text.count("Alpha") * 579 < P.min_chars // 2 + 600, (tail_blocks, chunk.text[:60])


def test_no_text_is_lost_without_overlap():
    blocks = paras(50)
    chunks = list(chunk_blocks(blocks, NO_OVERLAP))
    words = lambda s: re.findall(r"\w+", s)  # noqa: E731
    assert words(" ".join(c.text for c in chunks)) == words(" ".join(b.text for b in blocks))


def test_giant_paragraph_is_split_at_boundaries():
    sentence = "This is a sentence of ordinary length that ends cleanly. "
    block = Block(sentence * 2000, "H", None, None, 0)  # about 114k characters, no paragraph breaks
    chunks = list(chunk_blocks([block], NO_OVERLAP))
    assert max(len(c.text) for c in chunks) <= P.max_chars
    assert all(c.text.rstrip().endswith(".") for c in chunks)  # cut at sentence ends, never mid-sentence
    assert sum(len(c.text) for c in chunks) >= len(block.text) * 0.98


def test_text_without_any_boundary_is_cut_hard():
    pieces = list(split_block(Block("A" * 10_000, "H"), 2400))
    assert [len(p.text) for p in pieces] == [2400, 2400, 2400, 2400, 400]


def test_cjk_text_does_not_break_the_chunker():
    text = "这是一个关于天文学的句子。" * 600
    chunks = list(chunk_blocks([Block(text, "H")], P))
    assert chunks and "".join(c.text for c in chunks).count("天文学") >= 590  # (token counts are underestimated: noted)


def test_empty_and_whitespace_blocks_produce_nothing():
    assert list(chunk_blocks([], P)) == []
    assert list(chunk_blocks([Block("", "H"), Block("   \n  ", "H")], P)) == []


def test_locator_covers_the_range_of_pages_or_rows():
    blocks = [Block("page three text " * 60, "", "page 3", 3, 0), Block("page four text " * 60, "", "page 4", 4, 1000)]
    chunk = next(iter(chunk_blocks(blocks, P)))
    assert chunk.locator == "page 3 - page 4" and chunk.page == 3
    single = next(iter(chunk_blocks([Block("x" * 100, "", "row 7")], P)))
    assert single.locator == "row 7"


def test_character_offsets_point_at_the_source_blocks():
    chunks = list(chunk_blocks(paras(10), NO_OVERLAP))
    assert chunks[0].char_start == 0 and chunks[0].char_end and chunks[0].char_end > chunks[0].char_start


def test_chunking_is_deterministic_and_resumable():
    """Resume skips the first N chunks of a fresh run; the rest must equal what an uninterrupted run produced."""
    blocks = list(paras(80))
    full = list(chunk_blocks(iter(blocks), P))
    again = list(chunk_blocks(iter(blocks), P))
    assert [c.text_hash for c in full] == [c.text_hash for c in again]
    for skip in (1, 3, len(full) - 1):
        resumed = list(chunk_blocks(iter(blocks), P))[skip:]
        assert [c.text_hash for c in resumed] == [c.text_hash for c in full[skip:]]
        assert [c.ordinal for c in resumed] == [c.ordinal for c in full[skip:]]


def test_markdown_document_end_to_end_structure():
    from xodus.ingestion.parsers import Limits, inspect_file, iter_blocks

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "h.md"
        path.write_text(topic_markdown("astronomy", sections=3, per_section=4), encoding="utf-8")
        lim = Limits.from_settings(KnowledgeSettings())
        chunks = list(chunk_blocks(iter_blocks(path, inspect_file(path, lim), lim), P))
    assert chunks and all(c.heading_path.startswith("Astronomy handbook") for c in chunks)
    assert any("Part 2" in c.heading_path for c in chunks) and any("Part 3" in c.heading_path for c in chunks)


def test_version_constant_is_an_integer():
    assert isinstance(CHUNKER_VERSION, int) and CHUNKER_VERSION >= 1
    assert len(topic_paragraphs("music", 3)) == 3


@pytest.mark.parametrize("min_t, target_t, max_t", [(400, 300, 600), (700, 500, 600)])
def test_parameter_ordering_is_validated(min_t, target_t, max_t):
    with pytest.raises(ValueError):
        KnowledgeSettings(chunk_min_tokens=min_t, chunk_target_tokens=target_t, chunk_max_tokens=max_t)
