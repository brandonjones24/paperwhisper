"""Core sync engine: reMarkable ebook progress -> Audiobookshelf position.

Direction: ebook_to_audio (the safe one).
  * reMarkable side is READ-ONLY (parsed from rmfakecloud's blob store).
  * Audiobookshelf side is written via its API.

Position mapping is percentage-based: fraction_read(ebook) -> currentTime =
fraction * audiobook_duration. This is an approximation (narration pacing and
front/back matter differ between formats) and is best thought of as
"resume roughly where I left off", not word-accurate Whispersync.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .audiobookshelf import ABSItem, AudiobookshelfClient
from .config import Config
from .matcher import best_match
from .remarkable import RemarkableStore

log = logging.getLogger("paperwhisper.sync")


class State:
    """Tiny JSON state file: remembers the last ebook position we pushed per book,
    keyed by reMarkable uuid, so we only write ABS when the reader actually moves."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def last_page(self, uuid: str) -> int:
        return int(self.data.get(uuid, {}).get("last_opened_page", -1))

    def record(self, uuid: str, page: int, title: str, abs_id: str):
        self.data[uuid] = {"last_opened_page": page, "title": title, "abs_id": abs_id}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        except OSError as e:
            log.warning("could not persist state to %s: %s", self.path, e)


def run_once(cfg: Config) -> int:
    """One reconciliation pass. Returns number of ABS updates made (or that would
    be made in dry-run)."""
    store = RemarkableStore(cfg.rmfakecloud_data, cfg.rmfakecloud_user)
    abs_client = AudiobookshelfClient(cfg.abs_url, cfg.abs_token, verify_tls=cfg.abs_verify_tls)

    if not abs_client.ping():
        log.error("Audiobookshelf not reachable / token invalid; aborting pass")
        return 0

    audiobooks: list[ABSItem] = abs_client.audiobooks()
    log.info("Audiobookshelf: %d audiobooks", len(audiobooks))

    ebooks = store.books_with_progress()
    log.info("reMarkable: %d ebook(s) with reading progress", len(ebooks))

    state = State(cfg.state_file)
    updates = 0

    for book in ebooks:
        if book.progress < cfg.min_progress:
            continue

        match, s = best_match(
            book.title, book.author, audiobooks,
            key_title=lambda a: a.title, key_author=lambda a: a.author,
            threshold=cfg.match_threshold,
        )
        if not match:
            log.info("no audiobook match for %r (best score %.2f)", book.title, s)
            continue

        if match.duration <= 0:
            log.info("audiobook %r has no duration; skipping", match.title)
            continue

        # Skip if the reader hasn't moved since we last pushed this book.
        if state.last_page(book.uuid) == book.last_opened_page:
            log.debug("%r unchanged since last sync (page %d)", book.title, book.last_opened_page)
            continue

        target_time = book.progress * match.duration
        current = match.current_time
        delta_frac = abs(target_time - current) / match.duration if match.duration else 0.0

        log.info(
            "MATCH %r <-> %r (%.0f%%) | ebook %.1f%% -> %.0fs (abs now %.0fs, delta %.1f%%)",
            book.title, match.title, s * 100, book.progress * 100,
            target_time, current, delta_frac * 100,
        )

        if delta_frac < cfg.min_delta:
            log.debug("delta below MIN_DELTA; skipping write")
            state.record(book.uuid, book.last_opened_page, book.title, match.id)
            continue

        if cfg.dry_run:
            log.info("[DRY_RUN] would set %r to %.0fs", match.title, target_time)
        else:
            try:
                abs_client.set_progress(match.id, target_time, match.duration)
            except Exception as e:  # noqa: BLE001 - report and continue other books
                log.error("failed to update %r: %s", match.title, e)
                continue
        state.record(book.uuid, book.last_opened_page, book.title, match.id)
        updates += 1

    state.save()
    log.info("pass complete: %d update(s)%s", updates, " (dry-run)" if cfg.dry_run else "")
    return updates
