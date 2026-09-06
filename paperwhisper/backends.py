"""Read/write adapters so every backend is a peer."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Protocol

from .audiobookshelf import AudiobookshelfClient
from .mapping import MappingResult, PositionMapper, percent_to_page
from .peers import PeerBook
from .remarkable import RemarkableStore
from .remarkable_writer import ConflictError, RemarkableSyncWriter

log = logging.getLogger("paperwhisper.backends")


class Backend(Protocol):
    name: str
    writable: bool

    def books(self) -> list[PeerBook]: ...
    def epub_bytes(self, book: PeerBook) -> bytes | None: ...
    def apply(self, book: PeerBook, mapped: MappingResult) -> bool: ...


def _as_abs(book: PeerBook):
    return SimpleNamespace(
        id=book.ident,
        title=book.title,
        author=book.author,
        duration=book.duration,
        current_time=book.current_time,
        progress=book.progress,
    )


def _as_rm(book: PeerBook):
    return SimpleNamespace(
        uuid=book.ident,
        title=book.title,
        author=book.author,
        page_count=book.page_count,
        last_opened_page=book.last_opened_page,
        epub_hash=book.epub_hash,
        progress=book.progress,
    )


class AudiobookshelfBackend:
    name = "audiobookshelf"

    def __init__(self, client: AudiobookshelfClient):
        self.client = client
        self.writable = True

    def books(self) -> list[PeerBook]:
        out = []
        for it in self.client.audiobooks():
            out.append(
                PeerBook(
                    backend=self.name,
                    ident=it.id,
                    title=it.title,
                    author=it.author,
                    progress=float(it.progress or 0.0),
                    updated_ms=int(getattr(it, "last_update_ms", 0) or 0),
                    duration=float(it.duration or 0.0),
                    current_time=float(it.current_time or 0.0),
                )
            )
        return out

    def epub_bytes(self, book: PeerBook) -> bytes | None:
        return None

    def apply(self, book: PeerBook, mapped: MappingResult) -> bool:
        seconds = mapped.seconds
        if seconds is None:
            frac = mapped.frac if mapped.frac is not None else book.progress
            seconds = frac * book.duration if book.duration else 0.0
        if book.duration <= 0:
            log.warning("ABS %r has no duration; skip write", book.title)
            return False
        self.client.set_progress(book.ident, seconds, book.duration)
        return True


class RemarkableBackend:
    name = "remarkable"

    def __init__(self, store: RemarkableStore, writer: RemarkableSyncWriter | None):
        self.store = store
        self.writer = writer
        self.writable = writer is not None

    def books(self) -> list[PeerBook]:
        out = []
        for b in self.store.books():
            out.append(
                PeerBook(
                    backend=self.name,
                    ident=b.uuid,
                    title=b.title,
                    author=b.author,
                    progress=float(b.progress or 0.0),
                    updated_ms=int(b.last_modified_ms or 0),
                    page_count=int(b.page_count or 0),
                    last_opened_page=int(b.last_opened_page or 0),
                    epub_hash=b.epub_hash or "",
                )
            )
        return out

    def epub_bytes(self, book: PeerBook) -> bytes | None:
        return self.store.epub_bytes(_as_rm(book))

    def apply(self, book: PeerBook, mapped: MappingResult) -> bool:
        if self.writer is None:
            return False
        page = mapped.page
        if page is None:
            page = percent_to_page(mapped.frac or book.progress, book.page_count, page_lag=0)
        if book.page_count <= 0:
            log.warning("reMarkable %r has no page count; skip write", book.title)
            return False
        page = max(0, min(int(page), book.page_count - 1))
        for attempt in (1, 2):
            try:
                return self.writer.set_reading_position(book.ident, page)
            except ConflictError:
                log.info("generation conflict (attempt %d); retrying", attempt)
            except Exception as e:  # noqa: BLE001
                log.error("reMarkable write failed for %s: %s", book.ident, e)
                return False
        log.error("giving up on reMarkable doc %s after generation conflicts", book.ident)
        return False


class CalibreWebBackend:
    name = "calibreweb"

    def __init__(self, store):
        self.store = store
        self.writable = bool(getattr(store, "can_write", False))

    def books(self) -> list[PeerBook]:
        return list(self.store.peer_books())

    def epub_bytes(self, book: PeerBook) -> bytes | None:
        getter = getattr(self.store, "epub_bytes", None)
        return getter(book) if getter else None

    def apply(self, book: PeerBook, mapped: MappingResult) -> bool:
        frac = mapped.frac
        if frac is None and mapped.seconds is not None and book.duration:
            frac = mapped.seconds / book.duration
        if frac is None and mapped.page is not None and book.page_count:
            frac = mapped.page / book.page_count
        if frac is None:
            frac = book.progress
        frac = max(0.0, min(1.0, float(frac)))
        self.store.set_progress(book.ident, frac)
        return True


def map_leader_to_target(
    mapper: PositionMapper,
    leader: PeerBook,
    target: PeerBook,
    cluster: dict[str, PeerBook],
) -> MappingResult:
    """Map the leader's position onto a target backend, with lag on write."""
    abs_b = cluster.get("audiobookshelf")
    rm_b = cluster.get("remarkable")

    if target.backend == "audiobookshelf":
        if leader.backend == "remarkable" and rm_b is not None:
            result = mapper.page_to_seconds(_as_rm(leader), _as_abs(target))
        else:
            result = mapper.progress_to_seconds(leader.progress, target.duration)
        if result.frac is None and target.duration:
            result.frac = (result.seconds or 0.0) / target.duration
        return result

    if target.backend == "remarkable":
        if abs_b is not None and abs_b.duration > 0:
            if leader.backend == "audiobookshelf":
                item = _as_abs(leader)
            else:
                t = leader.progress * abs_b.duration
                item = SimpleNamespace(
                    id=abs_b.ident,
                    duration=abs_b.duration,
                    current_time=t,
                    progress=leader.progress,
                )
            result = mapper.audio_to_page(item, _as_rm(target))
        else:
            page = percent_to_page(leader.progress, target.page_count, page_lag=mapper.page_lag)
            result = MappingResult(page=page, frac=leader.progress, method="percent")
        if result.frac is None and target.page_count:
            result.frac = (result.page or 0) / target.page_count
        return result

    # calibreweb (percentage)
    frac = leader.progress
    if abs_b is not None and abs_b.duration > 0 and mapper.audio_lag:
        frac = max(0.0, frac - mapper.audio_lag / abs_b.duration)
    else:
        frac = max(0.0, frac - 0.005)
    return MappingResult(frac=frac, method="percent")


def target_delta(target: PeerBook, mapped: MappingResult) -> float:
    """How far the target would move, as a 0–1 fraction."""
    if target.backend == "audiobookshelf" and target.duration:
        seconds = mapped.seconds
        if seconds is None:
            seconds = (mapped.frac or 0.0) * target.duration
        return abs(seconds - target.current_time) / target.duration
    if target.backend == "remarkable" and target.page_count:
        page = mapped.page
        if page is None:
            page = percent_to_page(mapped.frac or 0.0, target.page_count, page_lag=0)
        return abs(page - target.last_opened_page) / target.page_count
    new_frac = mapped.frac if mapped.frac is not None else 0.0
    return abs(new_frac - target.progress)


def would_rewind(target: PeerBook, mapped: MappingResult) -> bool:
    if target.backend == "audiobookshelf":
        seconds = mapped.seconds
        if seconds is None:
            seconds = (mapped.frac or 0.0) * target.duration
        return seconds < target.current_time
    if target.backend == "remarkable":
        page = mapped.page
        if page is None:
            page = int((mapped.frac or 0.0) * target.page_count)
        return page < target.last_opened_page
    frac = mapped.frac if mapped.frac is not None else 0.0
    return frac < target.progress
