"""Calibre-Web reading-progress provider (KOReader ``kosync``).

Calibre-Web / calibre-web-automated stores KOReader sync progress in its
``app.db`` table ``kosync_progress``:

    user_id | document (KOReader partial-MD5 hash) | progress (xpointer) |
    percentage (0.0-1.0) | device | timestamp

The ``document`` is KOReader's *partial MD5* of the ebook file (md5 of 1 KiB
chunks read at 0, 1024, 4096, 16384, ... byte offsets). To turn it into a
title/author we compute the same hash for every file in the Calibre library
(paths + metadata come from Calibre's ``metadata.db``) and match.

Read-only: it only reads ``app.db`` and ``metadata.db``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3

from .providers import EbookProgress

log = logging.getLogger("paperwhisper.calibreweb")

_EBOOK_EXT = (".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".fb2", ".txt", ".djvu")


def koreader_partial_md5(path: str) -> str | None:
    """KOReader's partial-MD5 document hash.

    Reads 1 KiB at offsets 0, 1024, 4096, 16384, ... (``step << 2i`` for
    i = 0..10; i = -1 is offset 0, matching KOReader's 32-bit shift overflow).
    """
    step = size = 1024
    m = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for i in range(-1, 11):
                offset = 0 if i == -1 else (step << (2 * i))
                f.seek(offset)
                chunk = f.read(size)
                if not chunk:
                    break
                m.update(chunk)
    except OSError as e:
        log.debug("cannot hash %s: %s", path, e)
        return None
    return m.hexdigest()


def _connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)


def _split_libraries(value) -> list[str]:
    """CALIBRE_LIBRARY may name several libraries separated by ',' or os.pathsep."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = str(value or "").replace(os.pathsep, ",").split(",")
    return [p.strip() for p in parts if p.strip()]


class CalibreWebStore:
    def __init__(self, app_db: str, calibre_library, user_id: int | None = None):
        self.app_db = app_db
        self.libraries = _split_libraries(calibre_library)
        self.user_id = user_id
        self._hash_index: dict[str, tuple[str, str]] | None = None

    # -- map KOReader document hash -> (title, author) -------------------------

    def _library_files(self) -> list[tuple[str, str, str]]:
        """Return (filepath, title, author) for every book file across all libraries."""
        out: list[tuple[str, str, str]] = []
        for library in self.libraries:
            metadata_db = os.path.join(library, "metadata.db")
            try:
                con = _connect_ro(metadata_db)
            except sqlite3.Error as e:
                log.error("cannot open Calibre metadata.db at %s: %s", metadata_db, e)
                continue
            try:
                rows = con.execute(
                    """
                    SELECT b.title, b.path, d.name, d.format,
                           COALESCE(GROUP_CONCAT(a.name, ' & '), '')
                    FROM books b
                    JOIN data d ON d.book = b.id
                    LEFT JOIN books_authors_link bal ON bal.book = b.id
                    LEFT JOIN authors a ON a.id = bal.author
                    GROUP BY b.id, d.id
                    """
                ).fetchall()
            except sqlite3.Error as e:
                log.error("metadata.db query failed for %s: %s", library, e)
                continue
            finally:
                con.close()

            for title, path, name, fmt, author in rows:
                fpath = os.path.join(library, path, f"{name}.{str(fmt).lower()}")
                if os.path.exists(fpath) and fpath.lower().endswith(_EBOOK_EXT):
                    out.append((fpath, title, author))
        return out

    def _build_hash_index(self) -> dict[str, tuple[str, str]]:
        index: dict[str, tuple[str, str]] = {}
        for fpath, title, author in self._library_files():
            h = koreader_partial_md5(fpath)
            if h:
                index[h] = (title, author)
        log.info("Calibre-Web: hashed %d library files", len(index))
        return index

    def hash_index(self) -> dict[str, tuple[str, str]]:
        if self._hash_index is None:
            self._hash_index = self._build_hash_index()
        return self._hash_index

    # -- read progress ---------------------------------------------------------

    def progress_items(self) -> list[EbookProgress]:
        try:
            con = _connect_ro(self.app_db)
        except sqlite3.Error as e:
            log.error("cannot open Calibre-Web app.db at %s: %s", self.app_db, e)
            return []
        try:
            q = "SELECT document, percentage, user_id, timestamp FROM kosync_progress"
            params: tuple = ()
            if self.user_id is not None:
                q += " WHERE user_id = ?"
                params = (self.user_id,)
            rows = con.execute(q, params).fetchall()
        except sqlite3.Error as e:
            log.error("kosync_progress query failed: %s", e)
            return []
        finally:
            con.close()

        # keep the most recent row per document
        latest: dict[str, tuple[float, str]] = {}
        for document, percentage, _uid, ts in rows:
            if percentage is None:
                continue
            if document not in latest or (ts or "") > latest[document][1]:
                latest[document] = (float(percentage), ts or "")

        index = self.hash_index()
        items: list[EbookProgress] = []
        for document, (pct, _ts) in latest.items():
            meta = index.get(document)
            if not meta:
                log.debug("kosync document %s not matched to a library file", document)
                continue
            title, author = meta
            items.append(EbookProgress(ident=document, title=title, author=author,
                                       progress=max(0.0, min(1.0, pct))))
        return items
