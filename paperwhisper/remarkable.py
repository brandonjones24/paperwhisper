"""Read ebook reading progress out of an rmfakecloud data directory.

rmfakecloud stores each user's documents in a *content-addressed* blob store
(the "sync 1.5" format) under ``<data>/users/<user>/sync/``:

    root                     -> a single line: the hash of the root index blob
    <root-index-hash>        -> "3\\n" then one line per document:
                                  <doc-hash>:80000000:<uuid>:<count>:<size>
    <doc-hash>               -> "3\\n" then one line per file in that document:
                                  <file-hash>:0:<uuid>.<ext>:<count>:<size>
    <file-hash>              -> the actual file bytes (.metadata / .content JSON,
                                  .epub, .pdf, .pagedata, ...)

For a book we care about two JSON files:

    <uuid>.metadata  -> {"visibleName", "lastOpenedPage", "lastModified", ...}
    <uuid>.content   -> {"documentMetadata": {"title", "authors": [...]},
                          "fileType": "epub"|"pdf", "pageCount", ...}

Reading progress = ``lastOpenedPage / pageCount``.

This module is strictly READ-ONLY. It never writes into the blob store.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("paperwhisper.remarkable")

# Document file types we treat as "books" worth syncing.
BOOK_FILETYPES = {"epub", "pdf"}


@dataclass
class RemarkableBook:
    uuid: str
    title: str
    authors: list[str] = field(default_factory=list)
    file_type: str = "epub"
    page_count: int = 0
    last_opened_page: int = 0
    last_modified_ms: int = 0

    @property
    def progress(self) -> float:
        """Fraction read, 0.0-1.0. 0.0 if the page count is unknown."""
        if self.page_count and self.page_count > 0:
            return max(0.0, min(1.0, self.last_opened_page / self.page_count))
        return 0.0

    @property
    def author(self) -> str:
        return ", ".join(self.authors) if self.authors else ""


class RemarkableStore:
    """Parses a single user's rmfakecloud sync store. Read-only."""

    def __init__(self, data_dir: str | Path, user: str):
        self.sync_dir = Path(data_dir) / "users" / user / "sync"
        self.user = user

    # -- low level blob access -------------------------------------------------

    def _read_text(self, blob_hash: str) -> str | None:
        if not blob_hash:
            return None
        try:
            return (self.sync_dir / blob_hash).read_text(errors="replace")
        except OSError as e:
            log.debug("cannot read blob %s: %s", blob_hash, e)
            return None

    def _read_json(self, blob_hash: str) -> dict | None:
        raw = self._read_text(blob_hash)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_listing(text: str) -> list[list[str]]:
        """Parse a root-index or document listing blob into ``[fields, ...]``.

        The first line is a schema version ("3") which we skip. Each remaining
        line is colon-separated: ``hash:flag:name:count:size``.
        """
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.isdigit():  # skip blank + leading version int
                continue
            parts = line.split(":")
            if len(parts) >= 3:
                rows.append(parts)
        return rows

    # -- high level ------------------------------------------------------------

    def _document_hashes(self) -> list[str]:
        root_hash = self._read_text("root")
        if not root_hash:
            log.warning("no root file in %s", self.sync_dir)
            return []
        root_hash = root_hash.strip().splitlines()[0].strip()
        index = self._read_text(root_hash)
        if not index:
            log.warning("root index blob %s missing", root_hash)
            return []
        return [row[0] for row in self._parse_listing(index)]

    def _document_files(self, doc_hash: str) -> tuple[str, dict[str, str]]:
        """Return ``(uuid, {extension: file_hash})`` for one document."""
        listing = self._read_text(doc_hash)
        if not listing:
            return "", {}
        uuid = ""
        files: dict[str, str] = {}
        for row in self._parse_listing(listing):
            file_hash, name = row[0], row[2]
            if "." in name:
                stem, ext = name.rsplit(".", 1)
                files[ext.lower()] = file_hash
                uuid = uuid or stem
        return uuid, files

    def books(self) -> list[RemarkableBook]:
        """Return every book (epub/pdf) with parseable metadata."""
        out: list[RemarkableBook] = []
        for doc_hash in self._document_hashes():
            uuid, files = self._document_files(doc_hash)
            content = self._read_json(files.get("content", ""))
            meta = self._read_json(files.get("metadata", ""))
            if not content or not meta:
                continue
            file_type = str(content.get("fileType", "")).lower()
            if file_type not in BOOK_FILETYPES:
                continue  # skip folders / notebooks / other doc types

            dm = content.get("documentMetadata") or {}
            title = dm.get("title") or _strip_author(meta.get("visibleName", ""))
            authors = dm.get("authors") or []
            if isinstance(authors, str):
                authors = [authors]

            try:
                last_modified = int(meta.get("lastModified") or 0)
            except (ValueError, TypeError):
                last_modified = 0

            title = str(title).strip()
            if not title:
                continue

            out.append(
                RemarkableBook(
                    uuid=uuid or title,
                    title=title,
                    authors=[str(a).strip() for a in authors],
                    file_type=file_type,
                    page_count=int(content.get("pageCount") or content.get("originalPageCount") or 0),
                    last_opened_page=int(
                        meta.get("lastOpenedPage") or content.get("lastOpenedPage") or 0
                    ),
                    last_modified_ms=last_modified,
                )
            )
        return out

    def books_with_progress(self) -> list[RemarkableBook]:
        """Books the reader has actually opened past page 0."""
        return [b for b in self.books() if b.last_opened_page > 0 and b.progress > 0]

    def progress_items(self):
        """Shared EbookProgress view (for the ebook_to_audio provider interface)."""
        from .providers import EbookProgress
        return [
            EbookProgress(ident=b.uuid, title=b.title, author=b.author, progress=b.progress)
            for b in self.books_with_progress()
        ]


def _strip_author(visible_name: str) -> str:
    """``"Title - Author"`` visibleName -> best-effort title."""
    return visible_name.split(" - ")[0].strip() if visible_name else ""
