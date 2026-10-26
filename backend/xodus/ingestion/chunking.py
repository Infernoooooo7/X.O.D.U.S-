"""Structure-aware chunking (master prompt section 7: 400-600 tokens, small overlap).

Blocks (paragraphs, headings, table rows, PDF paragraphs) are packed greedily into chunks. A chunk ends
early at a heading change once it has reached the minimum size, so chunks follow the document's structure;
a block larger than the maximum is split at a paragraph, sentence or word boundary. Consecutive chunks of
the same section overlap by a few sentences' worth of text. The chunker is a pure function of the block
stream: the same file always yields the same chunks, which is what makes ingestion resumable.

Token counts are estimates (characters / chars_per_token); no tokenizer is available.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from xodus.config.settings import KnowledgeSettings
from xodus.ingestion.text import Block
from xodus.storage.knowledge import ChunkRecord

CHUNKER_VERSION = 1
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s")


@dataclass(frozen=True)
class ChunkParams:
    min_chars: int
    target_chars: int
    max_chars: int
    overlap_chars: int
    chars_per_token: float

    @classmethod
    def from_settings(cls, k: KnowledgeSettings) -> ChunkParams:
        cpt = k.chars_per_token
        return cls(int(k.chunk_min_tokens * cpt), int(k.chunk_target_tokens * cpt), int(k.chunk_max_tokens * cpt),
                   int(k.chunk_overlap_tokens * cpt), cpt)


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return max(1, math.ceil(len(text) / chars_per_token))


def split_block(block: Block, max_chars: int) -> Iterator[Block]:
    """Pieces of at most max_chars, cut at a newline, sentence end or space where one exists."""
    text, start = block.text, block.start
    while len(text) > max_chars:
        window = text[:max_chars]
        floor = max_chars // 2
        cut = window.rfind("\n", floor)
        if cut < 0:
            ends = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() >= floor]
            cut = ends[-1] if ends else -1
        if cut < 0:
            cut = window.rfind(" ", floor)
        if cut < 0:
            cut = max_chars  # no boundary at all (for example base64 or CJK text): cut hard
        piece, text = text[:cut].rstrip(), text[cut:].lstrip()
        if piece:
            yield Block(piece, block.heading, block.loc, block.page, start)
        start = None if start is None else start + cut
    if text.strip():
        yield Block(text, block.heading, block.loc, block.page, start)


def _overlap_tail(text: str, overlap_chars: int) -> str:
    """The last few sentences (up to overlap_chars) of a chunk, starting at a sentence or word boundary.

    A chunk no longer than the overlap itself gets none (it would just be repeated in full)."""
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:]
    boundary = next(iter(_SENTENCE_END.finditer(tail)), None)
    if boundary is not None and len(tail) - boundary.end() >= overlap_chars // 4:
        return tail[boundary.end():].strip()
    space = tail.find(" ")
    return tail[space + 1:].strip() if space >= 0 else ""


def chunk_blocks(blocks: Iterable[Block], params: ChunkParams) -> Iterator[ChunkRecord]:
    ordinal = 0
    parts: list[str] = []  # texts of the current chunk, joined by a blank line
    real = 0  # characters of the current chunk that are new (not carried over as overlap)
    length = 0
    first: Block | None = None
    last: Block | None = None
    pending_overlap = ""

    def emit() -> ChunkRecord | None:
        nonlocal ordinal
        assert first is not None and last is not None
        text = "\n\n".join(parts).strip()
        if real == 0 or not text:
            return None
        if first.loc is None:
            locator = None
        elif last.loc is None or last.loc == first.loc:
            locator = first.loc
        else:
            locator = f"{first.loc} - {last.loc}"
        end = (last.start or 0) + len(last.text) if last.start is not None else None
        record = ChunkRecord(
            ordinal=ordinal, text=text, text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            heading_path=first.heading, locator=locator, page=first.page, char_start=first.start, char_end=end,
            token_estimate=estimate_tokens(text, params.chars_per_token))
        ordinal += 1
        return record

    for block in blocks:
        for piece in split_block(block, params.max_chars):
            separator = 2 if parts else 0
            if parts:
                section_changed = piece.heading != (last.heading if last else piece.heading)
                size_after = length + separator + len(piece.text)
                if section_changed and real >= params.min_chars:
                    flush, carry = True, False
                elif size_after > params.target_chars and real >= params.min_chars:
                    flush, carry = True, True
                elif size_after > params.max_chars:
                    flush, carry = True, True
                else:
                    flush, carry = False, False
                if flush:
                    done = emit()
                    if done is not None:
                        yield done
                        pending_overlap = _overlap_tail(done.text, params.overlap_chars) if carry else ""
                    else:
                        pending_overlap = ""
                    parts, real, length, first, last = [], 0, 0, None, None
                    if pending_overlap:
                        parts, length = [pending_overlap], len(pending_overlap)
                        pending_overlap = ""
                    separator = 2 if parts else 0
            if first is None:
                first = piece
            parts.append(piece.text)
            real += len(piece.text)
            length += separator + len(piece.text)
            last = piece
    if first is not None:
        done = emit()
        if done is not None:
            yield done
