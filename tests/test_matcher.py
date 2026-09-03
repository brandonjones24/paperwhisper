"""Matcher regression tests (no fixtures needed). Run: python -m pytest -q"""
from paperwhisper.matcher import score

TH = 0.72
CASES = [
    ("The Martian: A Novel", "Andy Weir", "The Martian (Unabridged)", "Andy Weir", True),
    ("Artemis: A Novel", "Andy Weir", "Artemis", "Andy Weir", True),
    ("Harry Potter and the Sorcerer's Stone", "J.K. Rowling",
     "Harry Potter and the Sorcerer’s Stone (Full-Cast Edition)", "J.K. Rowling", True),
    # same author + shared series prefix must NOT match a different title
    ("Harry Potter and the Prisoner of Azkaban", "J.K. Rowling",
     "Harry Potter and the Sorcerer’s Stone (Full-Cast Edition)", "J.K. Rowling", False),
    ("Harry Potter and the Chamber of Secrets", "J.K. Rowling",
     "Harry Potter and the Goblet of Fire", "J.K. Rowling", False),
    ("Catching Fire", "Suzanne Collins", "The Hunger Games", "Suzanne Collins", False),
    ("A Game of Thrones", "George R. R. Martin", "A Game of Thrones", "George R.R. Martin", True),
    ("A Clash of Kings", "George R.R. Martin", "A Storm of Swords", "George R. R. Martin", False),
]


def test_matches():
    for rt, ra, at, aa, expected in CASES:
        got = score(rt, ra, at, aa) >= TH
        assert got == expected, f"{rt!r} <-> {at!r}: expected {expected}, got {got}"


if __name__ == "__main__":
    test_matches()
    print("ok")
