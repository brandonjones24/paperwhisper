"""Match a reMarkable ebook to an Audiobookshelf audiobook by title + author.

Titles differ across sources ("The Martian: A Novel" vs "The Martian"), so we
normalise aggressively and use a fuzzy ratio with an author tie-breaker.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Subtitle / edition noise we strip before comparing.
_NOISE = re.compile(
    r"\b(a novel|unabridged|abridged|the complete|complete|deluxe|"
    r"illustrated|special edition|edition|book \w+|vol\.?\s*\d+|volume \d+)\b",
    re.I,
)
_NONWORD = re.compile(r"[^a-z0-9 ]+")
_ARTICLES = re.compile(r"^(the|a|an) ")


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = text.split(":")[0]            # drop subtitle after a colon
    text = _NOISE.sub(" ", text)
    text = _NONWORD.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _ARTICLES.sub("", text)
    return text.strip()


def _author_key(name: str) -> set[str]:
    """Surnames-ish token set, order/format independent (handles 'Martin, George R. R.')."""
    tokens = _NONWORD.sub(" ", (name or "").lower()).split()
    return {t for t in tokens if len(t) > 1}


def title_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def author_overlap(a: str, b: str) -> float:
    ka, kb = _author_key(a), _author_key(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def score(rm_title: str, rm_author: str, abs_title: str, abs_author: str) -> float:
    """Combined 0-1 match score. Title dominates, author refines."""
    t = title_ratio(rm_title, abs_title)
    a = author_overlap(rm_author, abs_author)
    return round(0.75 * t + 0.25 * a, 4)


def best_match(rm_title, rm_author, candidates, key_title, key_author, threshold=0.72):
    """Return (best_candidate, score) above ``threshold`` or (None, best_score)."""
    best, best_s = None, 0.0
    for c in candidates:
        s = score(rm_title, rm_author, key_title(c), key_author(c))
        if s > best_s:
            best, best_s = c, s
    return (best, best_s) if best_s >= threshold else (None, best_s)
