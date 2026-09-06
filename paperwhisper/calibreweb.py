"""Calibre-Web reading-progress peer (KOReader ``kosync``).

Calibre-Web / calibre-web-automated stores KOReader sync progress in its
``app.db`` table ``kosync_progress``. The ``document`` is KOReader's partial
MD5 of the ebook file. Writes go through ``PUT /kosync/syncs/progress`` so
CWA side effects (read status, Kobo) still fire.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

from .peers import PeerBook
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


def _as_frac(pct: float) -> float:
    """CWA stores 0–100 after a kosync write; older rows / GET path use 0–1."""
    if pct > 1.0:
        pct = pct / 100.0
    return max(0.0, min(1.0, pct))


def _ts_ms(ts) -> int:
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        v = int(ts)
        return v if v > 1e12 else v * 1000
    s = str(ts).strip()
    if not s:
        return 0
    try:
        if s.isdigit():
            v = int(s)
            return v if v > 1e12 else v * 1000
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


class CalibreWebStore:
    def __init__(
        self,
        app_db: str,
        calibre_library,
        user_id: int | None = None,
        *,
        base_url: str = "",
        username: str = "",
        password: str = "",
        verify_tls: bool = True,
    ):
        self.app_db = app_db
        self.libraries = _split_libraries(calibre_library)
        self.user_id = user_id
        self.base_url = (base_url or "").rstrip("/") + ("/" if base_url else "")
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.can_write = bool(self.base_url and self.username and self.password)
        self._hash_index: dict[str, tuple[str, str, str]] | None = None
        self._id_index: dict[str, tuple[str, str, str]] | None = None

    # -- map KOReader document hash -> (title, author, path) -------------------

    def _library_files(self) -> list[tuple[str, str, str, str]]:
        """Return (book_id, filepath, title, author) for every library file."""
        out: list[tuple[str, str, str, str]] = []
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
                    SELECT b.id, b.title, b.path, d.name, d.format,
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

            for book_id, title, path, name, fmt, author in rows:
                fpath = os.path.join(library, path, f"{name}.{str(fmt).lower()}")
                if os.path.exists(fpath) and fpath.lower().endswith(_EBOOK_EXT):
                    out.append((str(book_id), fpath, title, author))
        return out

    def _build_indexes(self) -> None:
        hashes: dict[str, tuple[str, str, str]] = {}
        ids: dict[str, tuple[str, str, str]] = {}
        for book_id, fpath, title, author in self._library_files():
            rec = (title, author, fpath)
            ids.setdefault(book_id, rec)
            h = koreader_partial_md5(fpath)
            if h:
                hashes[h] = rec
        log.info("Calibre-Web: hashed %d library files", len(hashes))
        self._hash_index = hashes
        self._id_index = ids

    def hash_index(self) -> dict[str, tuple[str, str, str]]:
        if self._hash_index is None:
            self._build_indexes()
        return self._hash_index or {}

    def _lookup(self, document: str) -> tuple[str, str, str] | None:
        idx = self.hash_index()
        if document in idx:
            return idx[document]
        if self._id_index and document in self._id_index:
            return self._id_index[document]
        return None

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
        latest: dict[str, tuple[float, object]] = {}
        for document, percentage, _uid, ts in rows:
            if percentage is None:
                continue
            if document not in latest or _ts_ms(ts) >= _ts_ms(latest[document][1]):
                latest[document] = (float(percentage), ts)

        items: list[EbookProgress] = []
        for document, (pct, _ts) in latest.items():
            meta = self._lookup(str(document))
            if not meta:
                log.debug("kosync document %s not matched to a library file", document)
                continue
            title, author, _path = meta
            items.append(EbookProgress(ident=str(document), title=title, author=author,
                                       progress=_as_frac(pct)))
        return items

    def peer_books(self) -> list[PeerBook]:
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

        latest: dict[str, tuple[float, object]] = {}
        for document, percentage, _uid, ts in rows:
            if percentage is None:
                continue
            key = str(document)
            if key not in latest or _ts_ms(ts) >= _ts_ms(latest[key][1]):
                latest[key] = (float(percentage), ts)

        out: list[PeerBook] = []
        for document, (pct, ts) in latest.items():
            meta = self._lookup(document)
            if not meta:
                log.debug("kosync document %s not matched to a library file", document)
                continue
            title, author, path = meta
            out.append(
                PeerBook(
                    backend="calibreweb",
                    ident=document,
                    title=title,
                    author=author,
                    progress=_as_frac(pct),
                    updated_ms=_ts_ms(ts),
                    epub_hash=document if path.lower().endswith(".epub") else "",
                    extra={"path": path},
                )
            )
        return out

    def epub_bytes(self, book) -> bytes | None:
        path = ""
        if isinstance(book, PeerBook):
            path = (book.extra or {}).get("path") or ""
            if not path:
                meta = self._lookup(book.ident)
                path = meta[2] if meta else ""
        elif isinstance(book, str):
            meta = self._lookup(book)
            path = meta[2] if meta else ""
        if not path or not path.lower().endswith(".epub"):
            return None
        try:
            return open(path, "rb").read()
        except OSError as e:
            log.debug("cannot read epub %s: %s", path, e)
            return None

    def set_progress(self, document: str, percentage: float) -> None:
        """Write via CWA kosync HTTP so ReadBook / Kobo side effects still run."""
        if not self.can_write:
            raise RuntimeError("Calibre-Web write needs CWA_URL, CWA_USER, CWA_PASSWORD")
        frac = _as_frac(percentage)
        payload = {
            "document": document,
            "progress": f"frac:{frac:.5f}",
            "percentage": frac,
            "device": "paperwhisper",
            "device_id": "paperwhisper",
        }
        url = urljoin(self.base_url, "kosync/syncs/progress")
        r = requests.put(
            url,
            json=payload,
            auth=(self.username, self.password),
            timeout=20,
            verify=self.verify_tls,
        )
        r.raise_for_status()
        log.info("CWA progress set: document=%s -> %.1f%%", document, frac * 100)
