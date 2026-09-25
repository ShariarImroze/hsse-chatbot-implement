"""Deterministic intent helpers shared by dataset and chart planners."""

from __future__ import annotations

import re


_KEYWORD_CONTEXT = re.compile(
    r"\b(contain|contains|containing|mention|mentions|mentioning|word|words|keyword|keywords)\b",
    re.I,
)
_QUOTED_TEXT = re.compile(r"[\"“]([^\"“”]{1,100})[\"”]")
_UNQUOTED_ALTERNATIVES = re.compile(
    r"\b(?:contain|contains|containing|mention|mentions|mentioning)\b"
    r"(?:\s+the)?(?:\s+exact)?(?:\s+(?:word|words|keyword|keywords))?"
    r"\s+([A-Za-z][\w'-]{1,50})\s+or\s+([A-Za-z][\w'-]{1,50})\b",
    re.I,
)


def extract_explicit_keyword_search(text: str) -> tuple[str, str] | None:
    """Return explicitly quoted report keywords and their AND/OR mode."""

    if not _KEYWORD_CONTEXT.search(text):
        return None
    terms = [" ".join(match.split()) for match in _QUOTED_TEXT.findall(text)]
    terms = list(dict.fromkeys(term for term in terms if term))
    if not terms:
        alternatives = _UNQUOTED_ALTERNATIVES.search(text)
        if alternatives is None:
            return None
        terms = list(dict.fromkeys(alternatives.groups()))
    mode = "any" if len(terms) > 1 and re.search(r"\bor\b", text, re.I) else "all"
    return " ".join(terms), mode
