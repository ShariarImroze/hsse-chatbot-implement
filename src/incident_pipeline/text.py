"""Text normalization, language screening, and deterministic sampling helpers."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata


WORD_PATTERN = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)?")
WHITESPACE_PATTERN = re.compile(r"\s+")
NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
NEGATED_RELEASE_PATTERNS = (
    re.compile(
        r"\bno (?:known )?(?:(?:hazmat|oil|fuel|chemical|material|product|fluid) )?"
        r"(?:release|spill)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno signs? of (?:an? )?(?:release|spill)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:release|spill)s? (?:did|does|has|have|had) not occur\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:did not|does not|has not|have not) release\b", re.IGNORECASE),
    re.compile(r"\bnothing (?:was )?released\b", re.IGNORECASE),
    re.compile(r"\bwithout (?:an? )?(?:release|spill)\b", re.IGNORECASE),
)
POTENTIAL_RELEASE_PATTERNS = (
    re.compile(r"\bpotential (?:for (?:an? )?)?(?:release|spill)\b", re.IGNORECASE),
    re.compile(r"\b(?:release|spill) potential\b", re.IGNORECASE),
)
POSITIVE_RELEASE_PATTERNS = (
    re.compile(r"\breleased?\b", re.IGNORECASE),
    re.compile(r"\bspilled?\b", re.IGNORECASE),
    re.compile(r"\bdischarged?\b", re.IGNORECASE),
    re.compile(r"\bleaked?\b", re.IGNORECASE),
    re.compile(r"\boverflowed?\b", re.IGNORECASE),
    re.compile(r"\bvented?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:release|spill) (?:of|occurred|confirmed|observed|reported)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:visible |oily )?sheen\b", re.IGNORECASE),
)
ENGLISH_FUNCTION_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "could", "did",
    "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those", "through",
    "to", "too", "under", "until", "up", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with",
    "would", "you", "your", "yours", "yourself", "yourselves",
}

# High-frequency function words from the most likely Latin-script alternatives
# in the source data.  They are used only as a negative screen when several
# words from one language substantially outnumber English function words.
NON_ENGLISH_FUNCTION_WORDS = (
    frozenset(
        "al algo algunas algunos ante antes como con contra cual cuando de del "
        "desde donde durante e el ella ellas ellos en entre era eran es esa esas "
        "ese eso esos esta estaba estaban este esto estos fue fueron hasta la las "
        "lo los mas muy ni no o para pero por porque que se sin sobre su sus un "
        "una unas uno unos y ya".split()
    ),
    frozenset(
        "au aux avec ce ces comme dans de des du elle elles en entre est et eux "
        "il ils je la le les leur leurs lui mais ne nous on ou par pas pour que "
        "qui sans se ses son sont sous sur tu un une vous".split()
    ),
    frozenset(
        "aber als am an auf aus bei bis das dem den der des die durch ein eine "
        "einer eines er es fuer hat im in ist mit nach nicht oder ohne sie sind "
        "ueber um und unter von vor war waren was wie wir zu zum zur".split()
    ),
    frozenset(
        "aos com como da das de do dos e ela elas ele eles em entre era eram essa "
        "esse esta foi foram mais mas na nas no nos nao o os ou para pela pelo por "
        "que se sem ser seu seus sua suas um uma".split()
    ),
    frozenset(
        "al alla alle anche che chi come con da dal dalla delle di e era erano gli "
        "ha hanno il in la le lo ma nel nella non o per piu quale quando se senza "
        "sono su tra un una uno".split()
    ),
    frozenset(
        "aan als bij dat de den der des deze die dit door een en er had hebben het "
        "hij in is maar met naar niet of om onder op over te tot uit van voor was "
        "waren wat we zij zijn".split()
    ),
)


def clean_text(value: object) -> str:
    """Normalize source text without altering its substantive content."""
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\x00", " ")
    text = unicodedata.normalize("NFKC", text)
    return WHITESPACE_PATTERN.sub(" ", text).strip(" \t\r\n;,.|")


def combine_distinct(parts: list[object]) -> str:
    """Join non-empty source fields while suppressing exact repeated fragments."""
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = clean_text(part)
        if not cleaned:
            continue
        key = normalized_for_hash(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        if cleaned[-1:] not in ".!?":
            cleaned += "."
        result.append(cleaned)
    return " ".join(result)


def word_count(text: str) -> int:
    return len(WORD_PATTERN.findall(text))


def is_probably_english(text: str, minimum_source_words: int = 5) -> bool:
    """Dependency-free high-precision screen for the English-only corpus.

    Source language declarations remain the primary evidence.  This secondary
    screen rejects non-Latin text and sustained Spanish, French, German,
    Portuguese, Italian, or Dutch function-word patterns while retaining terse
    English technical reports.
    """
    words = [word.lower() for word in WORD_PATTERN.findall(text)]
    if len(words) < minimum_source_words:
        return False
    alphabetic = [character for character in text if character.isalpha()]
    if not alphabetic:
        return False
    ascii_ratio = sum(character.isascii() for character in alphabetic) / len(alphabetic)
    if ascii_ratio < 0.90:
        return False
    english_hits = sum(word in ENGLISH_FUNCTION_WORDS for word in words)
    if english_hits < 1:
        return False
    strongest_alternative = max(
        sum(word in vocabulary for word in words)
        for vocabulary in NON_ENGLISH_FUNCTION_WORDS
    )
    return not (
        strongest_alternative >= 4
        and strongest_alternative > english_hits * 1.5
    )


def normalized_for_hash(text: str) -> str:
    return NON_WORD_PATTERN.sub(" ", clean_text(text).lower()).strip()


def text_digest(text: str) -> str:
    return hashlib.sha256(normalized_for_hash(text).encode("utf-8")).hexdigest()


def is_explicit_non_release(text: str) -> bool:
    """Identify reports that describe only a potential or absent release.

    Negated phrases are removed before positive evidence is evaluated, so a
    real land spill followed by "no release to waterways" remains eligible.
    """

    cleaned = clean_text(text)
    negative = any(pattern.search(cleaned) for pattern in NEGATED_RELEASE_PATTERNS)
    potential = any(pattern.search(cleaned) for pattern in POTENTIAL_RELEASE_PATTERNS)
    if not negative and not potential:
        return False
    remaining = cleaned
    for pattern in (*NEGATED_RELEASE_PATTERNS, *POTENTIAL_RELEASE_PATTERNS):
        remaining = pattern.sub(" ", remaining)
    return not any(pattern.search(remaining) for pattern in POSITIVE_RELEASE_PATTERNS)


def sampling_rank(seed: int, source: str, record_id: str) -> int:
    material = f"{seed}|{source}|{record_id}".encode("utf-8")
    # Keep the rank within SQLite's signed 64-bit INTEGER range.
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & ((1 << 63) - 1)


def stable_split(case_no: str, train: float = 0.8, validation: float = 0.1) -> str:
    value = int.from_bytes(hashlib.sha256(case_no.encode("utf-8")).digest()[:8], "big")
    fraction = value / (2**64 - 1)
    if fraction < train:
        return "train"
    if fraction < train + validation:
        return "validation"
    return "test"
