"""Ebook reading-progress providers.

An ebook progress *provider* answers one question for the ``ebook_to_audio``
direction: "which books has the reader made progress in, and how far?"

    progress_items() -> list[EbookProgress]

reMarkable/rmfakecloud is one provider; Calibre-Web (KOReader ``kosync``) is
another, for the much larger crowd who read on KOReader/Kindle/Kobo and don't
own a reMarkable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EbookProgress:
    ident: str        # stable key for state tracking (uuid, hash, book id...)
    title: str
    author: str
    progress: float   # 0.0-1.0
