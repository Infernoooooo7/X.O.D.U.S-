"""Text clean-up and the Block unit that parsers produce and the chunker consumes."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Removed from all extracted text: NUL and other C0 controls (tab and newline are kept), DEL, C1 controls, and
# the bidirectional override/isolate controls that can make text read differently from how it is stored.
_STRIP = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_STRIP.update({c: None for c in range(0x7F, 0xA0)})
_STRIP.update({c: None for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))})


@dataclass(frozen=True)
class Block:
    """A unit of source text with where it came from. The chunker packs blocks into chunks."""

    text: str
    heading: str = ""  # heading path, e.g. "Install > Windows"
    loc: str | None = None  # human-readable place: "page 3", "row 12"
    page: int | None = None
    start: int | None = None  # character offset in the extracted text


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text.translate(_STRIP)


def collapse_blank_lines(text: str) -> str:
    out: list[str] = []
    blank = 0
    for line in text.split("\n"):
        line = line.rstrip()
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()
