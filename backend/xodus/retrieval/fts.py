"""Turning free text into a safe FTS5 query.

FTS5 has its own query language (AND/OR/NOT/NEAR, column filters, prefix stars, quotes). Passing user or model
text to MATCH directly lets that text change the query or raise syntax errors. Here the text is reduced to
plain word tokens, each wrapped in double quotes (a quoted token is always a literal), joined with OR.
"""

from __future__ import annotations

import re

MAX_TERMS = 12
_WORD = re.compile(r"\w+", re.UNICODE)
STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor not now of off on once only or other
our ours ourselves out over own same she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself yourselves
""".split())


def query_terms(query: str, max_terms: int = MAX_TERMS) -> list[str]:
    """Lower-cased distinct word tokens without stop words (a query made only of stop words keeps them)."""
    tokens = [t.lower() for t in _WORD.findall(query)]
    kept = [t for t in tokens if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
    terms = kept or tokens
    seen: dict[str, None] = {}
    for term in terms:
        seen.setdefault(term, None)
    return list(seen)[:max_terms]


def build_match(terms: list[str]) -> str:
    """'"alpha" OR "beta"'. Terms consist of word characters only, so no quote can occur inside one."""
    return " OR ".join(f'"{t}"' for t in terms if t and all(c.isalnum() or c == "_" for c in t))


def count_matching_terms(text: str, terms: list[str]) -> int:
    words = {w.lower() for w in _WORD.findall(text)}
    return sum(1 for t in terms if t in words)
