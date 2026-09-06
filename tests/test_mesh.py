"""Mesh sync writes laggards toward the furthest-ahead peer."""
import os
from paperwhisper.peers import PeerBook
from paperwhisper.sync import run_once


class FakeBackend:
    def __init__(self, name, books, writable=True):
        self.name = name
        self._books = books
        self.writable = writable
        self.applied = []

    def books(self):
        return list(self._books)

    def epub_bytes(self, _book):
        return None

    def apply(self, book, mapped):
        self.applied.append((book.ident, mapped))
        return True


def _cfg(tmp_path, **extra):
    os.environ["ABS_URL"] = "http://audiobookshelf:13378"
    os.environ["ABS_TOKEN"] = "t"
    os.environ["RMFAKECLOUD_USER"] = "user"
    os.environ["DIRECTION"] = extra.pop("direction", "all")
    os.environ["DRY_RUN"] = "false"
    os.environ["STATE_FILE"] = str(tmp_path / "state.json")
    os.environ["MIN_DELTA"] = "0.01"
    os.environ["MIN_PAGE_DELTA"] = "1"
    os.environ["ALLOW_REWIND"] = extra.get("allow_rewind", "false")
    from paperwhisper.config import Config
    cfg = Config()
    cfg.dry_run = False
    cfg.allow_rewind = extra.get("allow_rewind", "false") == "true"
    return cfg


def _book(backend, title, progress, ident=None):
    return PeerBook(
        backend=backend,
        ident=ident or f"{backend}-1",
        title=title,
        author="Andy Weir",
        progress=progress,
        page_count=100 if backend == "remarkable" else 0,
        last_opened_page=int(progress * 100) if backend == "remarkable" else 0,
        duration=10000 if backend == "audiobookshelf" else 0,
        current_time=progress * 10000 if backend == "audiobookshelf" else 0,
    )


def test_remarkable_ahead_updates_abs_and_cwa(tmp_path):
    rm = FakeBackend("remarkable", [_book("remarkable", "The Martian", 0.60)])
    abs_ = FakeBackend("audiobookshelf", [_book("audiobookshelf", "The Martian", 0.20)])
    cwa = FakeBackend("calibreweb", [_book("calibreweb", "The Martian", 0.10)])
    cfg = _cfg(tmp_path)
    n = run_once(cfg, {"remarkable": rm, "audiobookshelf": abs_, "calibreweb": cwa})
    assert n == 2
    assert abs_.applied
    assert cwa.applied
    assert not rm.applied  # leader is not written


def test_rewind_protect_skips_target_ahead(tmp_path):
    rm = FakeBackend("remarkable", [_book("remarkable", "The Martian", 0.20)])
    abs_ = FakeBackend("audiobookshelf", [_book("audiobookshelf", "The Martian", 0.80)])
    cfg = _cfg(tmp_path, direction="ebook_to_audio")
    n = run_once(cfg, {"remarkable": rm, "audiobookshelf": abs_})
    assert n == 0
    assert not abs_.applied


def test_audio_to_ebook_only_writes_ebook(tmp_path):
    rm = FakeBackend("remarkable", [_book("remarkable", "The Martian", 0.10)])
    abs_ = FakeBackend("audiobookshelf", [_book("audiobookshelf", "The Martian", 0.50)])
    cwa = FakeBackend("calibreweb", [_book("calibreweb", "The Martian", 0.10)])
    cfg = _cfg(tmp_path, direction="audio_to_ebook")
    n = run_once(cfg, {"remarkable": rm, "audiobookshelf": abs_, "calibreweb": cwa})
    assert n == 2
    assert not abs_.applied
    assert rm.applied
    assert cwa.applied
