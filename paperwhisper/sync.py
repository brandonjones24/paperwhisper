"""Core sync engine.

Two directions, dispatched by ``Config.direction``:

  ebook_to_audio  reMarkable reading position -> Audiobookshelf listen position
                  (reMarkable read-only; ABS written via API)

  audio_to_ebook  Audiobookshelf listen position -> reMarkable reading position
                  (ABS read-only; reMarkable written via rmfakecloud sync API)

Position mapping is percentage-based: fraction(source) maps to the target's
own scale (audiobook seconds, or ebook pages). This is an approximation —
"resume roughly where I left off", not word-accurate Whispersync.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .audiobookshelf import ABSItem, AudiobookshelfClient
from .config import Config
from .matcher import best_match
from .remarkable import RemarkableBook, RemarkableStore
from .remarkable_writer import ConflictError, RemarkableSyncWriter

log = logging.getLogger("paperwhisper.sync")


class State:
    """Tiny JSON state file so we only write when the source actually moved."""

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            self.data: dict[str, dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict:
        return self.data.get(key, {})

    def record(self, key: str, **fields):
        self.data[key] = fields

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        except OSError as e:
            log.warning("could not persist state to %s: %s", self.path, e)


def _match_ab(book, audiobooks: list[ABSItem], threshold: float):
    return best_match(
        book.title, book.author, audiobooks,
        key_title=lambda a: a.title, key_author=lambda a: a.author,
        threshold=threshold,
    )


def get_ebook_provider(cfg: Config):
    """The ebook reading-progress source for ebook_to_audio."""
    if cfg.ebook_provider == "calibreweb":
        from .calibreweb import CalibreWebStore
        return CalibreWebStore(cfg.cwa_app_db, cfg.calibre_library, cfg.cwa_user_id or None)
    return RemarkableStore(cfg.rmfakecloud_data, cfg.rmfakecloud_user)


def run_once(cfg: Config) -> int:
    if cfg.direction == "audio_to_ebook":
        return _run_audio_to_ebook(cfg)
    return _run_ebook_to_audio(cfg)


# --------------------------------------------------------------------------- #
# ebook -> audio                                                              #
# --------------------------------------------------------------------------- #
def _run_ebook_to_audio(cfg: Config) -> int:
    provider = get_ebook_provider(cfg)
    abs_client = AudiobookshelfClient(cfg.abs_url, cfg.abs_token, verify_tls=cfg.abs_verify_tls)
    if not abs_client.ping():
        log.error("Audiobookshelf not reachable / token invalid; aborting pass")
        return 0

    audiobooks = abs_client.audiobooks()
    ebooks = provider.progress_items()  # [EbookProgress(ident, title, author, progress)]
    log.info("ebook->audio (%s): %d audiobooks, %d ebook(s) with progress",
             cfg.ebook_provider, len(audiobooks), len(ebooks))

    state = State(cfg.state_file)
    updates = 0
    for book in ebooks:
        if book.progress < cfg.min_progress:
            continue
        match, s = _match_ab(book, audiobooks, cfg.match_threshold)
        if not match or match.duration <= 0:
            if not match:
                log.info("no audiobook match for %r (best %.2f)", book.title, s)
            continue
        frac = round(book.progress, 4)
        if self_unchanged(state, book.ident, "ebook_frac", frac):
            continue

        target = book.progress * match.duration
        delta = abs(target - match.current_time) / match.duration
        log.info("MATCH %r<->%r (%.0f%%) ebook %.1f%% -> %.0fs (abs %.0fs, d%.1f%%)",
                 book.title, match.title, s * 100, book.progress * 100, target, match.current_time, delta * 100)
        if not cfg.allow_rewind and target < match.current_time:
            log.info("skip: would rewind %r (%.0fs < %.0fs); set ALLOW_REWIND=true to override",
                     match.title, target, match.current_time)
            state.record(book.ident, ebook_frac=frac)
            continue
        if delta >= cfg.min_delta:
            if cfg.dry_run:
                log.info("[DRY_RUN] would set %r to %.0fs", match.title, target)
            else:
                try:
                    abs_client.set_progress(match.id, target, match.duration)
                except Exception as e:  # noqa: BLE001
                    log.error("failed to update %r: %s", match.title, e)
                    continue
            updates += 1
        state.record(book.ident, ebook_frac=frac)
    state.save()
    log.info("pass complete: %d update(s)%s", updates, " (dry-run)" if cfg.dry_run else "")
    return updates


# --------------------------------------------------------------------------- #
# audio -> ebook                                                              #
# --------------------------------------------------------------------------- #
def _run_audio_to_ebook(cfg: Config) -> int:
    store = RemarkableStore(cfg.rmfakecloud_data, cfg.rmfakecloud_user)
    abs_client = AudiobookshelfClient(cfg.abs_url, cfg.abs_token, verify_tls=cfg.abs_verify_tls)
    if not abs_client.ping():
        log.error("Audiobookshelf not reachable / token invalid; aborting pass")
        return 0

    audiobooks = abs_client.audiobooks()
    # need a page count to map a percentage to a page
    ebooks = [b for b in store.books() if b.page_count > 0]
    log.info("audio->ebook: %d audiobooks, %d ebook(s) with page counts", len(audiobooks), len(ebooks))

    writer = RemarkableSyncWriter(cfg.rmfakecloud_url, cfg.rmfakecloud_device_token)
    state = State(cfg.state_file)
    updates = 0

    for book in ebooks:
        match, s = _match_ab(book, audiobooks, cfg.match_threshold)
        if not match or match.progress < cfg.min_progress:
            continue

        target_page = round(match.progress * book.page_count)
        if abs(target_page - book.last_opened_page) < cfg.min_page_delta:
            continue
        if not cfg.allow_rewind and target_page < book.last_opened_page:
            log.info("skip: would rewind %r (page %d < %d); set ALLOW_REWIND=true to override",
                     book.title, target_page, book.last_opened_page)
            continue
        # skip if we already pushed this audiobook position for this book
        if self_unchanged(state, book.uuid, "abs_current", round(match.current_time)):
            continue

        log.info("MATCH %r<->%r (%.0f%%) audio %.1f%% -> ebook page %d/%d (was %d)",
                 match.title, book.title, s * 100, match.progress * 100,
                 target_page, book.page_count, book.last_opened_page)

        if cfg.dry_run:
            log.info("[DRY_RUN] would set %r to page %d", book.title, target_page)
            updates += 1
        else:
            if _write_position(writer, book.uuid, target_page):
                updates += 1
            else:
                continue
        state.record(book.uuid, abs_current=round(match.current_time), ebook_page=target_page)

    state.save()
    log.info("pass complete: %d update(s)%s", updates, " (dry-run)" if cfg.dry_run else "")
    return updates


def _write_position(writer: RemarkableSyncWriter, doc_id: str, page: int) -> bool:
    """Write with one retry on a generation conflict (someone else synced)."""
    for attempt in (1, 2):
        try:
            return writer.set_reading_position(doc_id, page)
        except ConflictError:
            log.info("generation conflict (attempt %d); re-reading root and retrying", attempt)
        except Exception as e:  # noqa: BLE001
            log.error("write failed for doc %s: %s", doc_id, e)
            return False
    log.error("giving up on doc %s after generation conflicts", doc_id)
    return False


def self_unchanged(state: State, key: str, field: str, value) -> bool:
    return state.get(key).get(field) == value
