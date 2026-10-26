"""Citations: the answer must cite passages as [chunk:ID]; a validator rejects ids that were not in the context.

Rejection is in code and does not depend on the model behaving: every cited id is checked against the ids of the
evidence that was actually given to the model for this request. A model that cites a chunk it was never shown
(invented, from another collection, from an earlier request) gets that citation removed and reported.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

INSUFFICIENT_PHRASE = "The provided documents do not contain enough information to answer this."
_GROUP = re.compile(r"\[\s*chunk\s*:\s*([0-9]{1,18}(?:\s*,\s*(?:chunk\s*:\s*)?[0-9]{1,18})*)\s*\]", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]{1,18}")


@dataclass(frozen=True)
class CitationReport:
    valid: list[int]
    invalid: list[int]
    cleaned: str
    uncited: bool  # an answer was given but cites nothing valid
    insufficient: bool  # the model said the documents do not contain the answer


def extract_citations(text: str) -> list[int]:
    """Every id cited in the text, in order of first appearance."""
    seen: dict[int, None] = {}
    for group in _GROUP.finditer(text):
        for number in _NUMBER.findall(group.group(1)):
            seen.setdefault(int(number), None)
    return list(seen)


def validate_answer(answer: str, context_ids: Collection[int]) -> CitationReport:
    context = set(context_ids)
    valid: dict[int, None] = {}
    invalid: dict[int, None] = {}

    def rewrite(match: re.Match[str]) -> str:
        ids = [int(n) for n in _NUMBER.findall(match.group(1))]
        keep = [i for i in ids if i in context]
        for i in ids:
            (valid if i in context else invalid).setdefault(i, None)
        if not keep:
            return "[citation removed]"
        return "[" + ", ".join(f"chunk:{i}" for i in dict.fromkeys(keep)) + "]"

    cleaned = _GROUP.sub(rewrite, answer)
    normalised = " ".join(answer.split()).lower()
    insufficient = INSUFFICIENT_PHRASE.lower() in normalised
    return CitationReport(list(valid), list(invalid), cleaned, uncited=bool(context) and not valid and not insufficient,
                          insufficient=insufficient)
