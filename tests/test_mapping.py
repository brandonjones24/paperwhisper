"""Chapter alignment + conservative floor/lag mapping."""
from __future__ import annotations

from types import SimpleNamespace

from paperwhisper.mapping import (
    AudioChapter,
    EbookChapter,
    PositionMapper,
    align_chapters,
    chapter_number,
    chapter_score,
    is_skippable,
    map_page_to_seconds,
    map_time_to_page,
    percent_to_page,
    percent_to_seconds,
    title_core,
    toc_to_ebook_chapters,
)
from paperwhisper.epubtoc import TocEntry


def test_chapter_number_variants():
    assert chapter_number("Chapter 5 - SOL") == 5
    assert chapter_number("CHAPTER ONE") == 1
    assert chapter_number("Ch. 12") == 12
    assert chapter_number("Chapter XVII: The Man with Two Faces") == 17
    assert chapter_number("3. The Riddle House") == 3
    assert chapter_number("The Riddle House") is None
    assert chapter_number("Opening Credits") is None


def test_title_core_strips_chapter_prefix():
    assert title_core("Chapter 1 - The Boy Who Lived") == "the boy who lived"
    assert title_core("Chapter One — The Boy Who Lived") == "the boy who lived"
    assert title_core("1. The Riddle House") == "the riddle house"
    assert title_core("The Riddle House") == "the riddle house"
    # don't eat a real title that just starts with "Chapter"
    assert "boy" in title_core("Chapter The Boy Who Lived")


def test_skippable_credits_not_prologue():
    assert is_skippable("Opening Credits")
    assert is_skippable("End Credits")
    assert is_skippable("Dedication")
    assert is_skippable("About the Author")
    assert not is_skippable("Prologue")
    assert not is_skippable("Chapter 1")
    assert not is_skippable("The Riddle House")


def test_different_numbers_never_match():
    assert chapter_score("Chapter 1", "Chapter 11") == 0.0
    assert chapter_score("Chapter 5 - SOL", "Chapter 5") >= 0.9


def test_hp_full_cast_pairs_seventeen_body_chapters():
    audio = [AudioChapter("Opening Credits", 0, 30)]
    for i in range(1, 18):
        audio.append(AudioChapter(f"Chapter {i} - Title {i}", 30 + (i - 1) * 100, 30 + i * 100))
    audio.append(AudioChapter("End Credits", 1730, 1760))

    ebook = [
        EbookChapter("Title Page", 0, 2),
        EbookChapter("Dedication", 2, 3),
    ]
    for i in range(1, 18):
        ebook.append(EbookChapter(f"Chapter {i}", 3 + (i - 1) * 10, 3 + i * 10))

    aligned = align_chapters(audio, ebook)
    assert aligned is not None
    assert len(aligned) == 17
    assert aligned[0].title.startswith("Chapter 1")
    assert aligned[0].audio_start == 30
    assert aligned[-1].page_start == 3 + 16 * 10


def test_named_goblet_chapters_match_leading_numbers():
    names = ["The Riddle House", "The Scar", "The Invitation", "Back to the Burrow"]
    audio = [AudioChapter(n, i * 100, (i + 1) * 100) for i, n in enumerate(names)]
    ebook = [EbookChapter(f"{i + 1}. {n}", i * 20, (i + 1) * 20) for i, n in enumerate(names)]
    aligned = align_chapters(audio, ebook)
    assert aligned is not None
    assert [a.title for a in aligned] == [f"{i + 1}. {n}" for i, n in enumerate(names)]


def test_hp_audio_numbers_match_named_ebook_toc():
    titles = ["The Boy Who Lived", "The Vanishing Glass", "The Letters from No One", "The Keeper of the Keys"]
    audio = [AudioChapter("Opening Credits", 0, 20)]
    ebook = [EbookChapter("Dedication", 0, 4)]
    for i, t in enumerate(titles):
        audio.append(AudioChapter(f"Chapter {i + 1} - {t}", 20 + i * 80, 20 + (i + 1) * 80))
        ebook.append(EbookChapter(t, 4 + i * 15, 4 + (i + 1) * 15))
    aligned = align_chapters(audio, ebook)
    assert aligned is not None
    assert [a.title for a in aligned] == titles


def test_percent_floor_and_lag_lands_behind():
    # old behaviour: round(0.194 * 377) = 73; we want behind that
    assert percent_to_page(0.194, 377, page_lag=1) < round(0.194 * 377)
    assert percent_to_page(0.5, 508, page_lag=1) == 253  # floor(254) - 1? floor(0.5*508)=254, -1=253
    assert percent_to_page(0.0, 508, page_lag=1) == 0
    assert percent_to_seconds(0.5, 10000, audio_lag=15) == 5000 - 15


def test_map_time_stays_in_matched_chapter():
    pairs = align_chapters(
        [
            AudioChapter("Opening Credits", 0, 20),
            AudioChapter("Chapter 1", 20, 120),
            AudioChapter("Chapter 2", 120, 220),
        ],
        [
            EbookChapter("Dedication", 0, 5),
            EbookChapter("Chapter 1", 20, 80),
            EbookChapter("Chapter 2", 80, 140),
        ],
    )
    assert pairs is not None
    # 50% through chapter 1 audio (70s), minus 15s lag → 55s → 35/100 into ch1
    result = map_time_to_page(70, 220, 140, pairs, page_lag=1, audio_lag=15)
    assert result.method == "chapter"
    assert result.chapter_title == "Chapter 1"
    assert 20 <= result.page < 80


def test_opening_credits_land_in_front_matter():
    pairs = align_chapters(
        [AudioChapter("Opening Credits", 0, 30), AudioChapter("Chapter 1", 30, 130)],
        [EbookChapter("Chapter 1", 10, 50)],
    )
    result = map_time_to_page(10, 130, 50, pairs, page_lag=1, audio_lag=15)
    assert result.method == "chapter"
    assert result.page < 10


def test_page_to_seconds_stays_in_chapter():
    pairs = align_chapters(
        [AudioChapter("Chapter 1", 0, 100), AudioChapter("Chapter 2", 100, 200)],
        [EbookChapter("Chapter 1", 0, 50), EbookChapter("Chapter 2", 50, 100)],
    )
    result = map_page_to_seconds(60, 100, 200, pairs, page_lag=1, audio_lag=15)
    assert result.method == "chapter"
    assert result.chapter_title == "Chapter 2"
    assert 100 <= result.seconds < 200
    # lag pulls it behind the raw interpolation
    raw = 100 + ((60 - 50) / 50) * 100
    assert result.seconds == raw - 15


def test_unaligned_falls_back_to_percent():
    result = map_time_to_page(500, 1000, 200, None, page_lag=1, audio_lag=15)
    assert result.method == "percent"
    # (500-15)/1000 * 200 = 97 → floor 97 - 1 = 96
    assert result.page == percent_to_page((500 - 15) / 1000, 200, page_lag=1)


def test_toc_to_pages_uses_fractions():
    toc = [
        TocEntry("Ch 1", "a", 0.0, 0.5),
        TocEntry("Ch 2", "b", 0.5, 1.0),
    ]
    chs = toc_to_ebook_chapters(toc, 100)
    assert chs[0].start_page == 0
    assert chs[0].end_page == 50
    assert chs[1].end_page == 100


def test_position_mapper_uses_loaders():
    audio = [
        AudioChapter("Opening Credits", 0, 10),
        AudioChapter("Chapter 1", 10, 110),
        AudioChapter("Chapter 2", 110, 210),
        AudioChapter("Chapter 3", 210, 310),
    ]
    toc = [
        TocEntry("Chapter 1", "c1", 0.1, 0.4),
        TocEntry("Chapter 2", "c2", 0.4, 0.7),
        TocEntry("Chapter 3", "c3", 0.7, 1.0),
    ]

    mapper = PositionMapper(
        chapter_map=True,
        page_lag=1,
        audio_lag=15,
        chapter_loader=lambda _id: audio,
        epub_loader=lambda _book: b"",  # unused; we stub _toc
    )
    mapper._toc_cache["hash"] = toc
    item = SimpleNamespace(id="abs1", duration=310, current_time=160)  # in ch2
    book = SimpleNamespace(page_count=100, epub_hash="hash", title="Book", last_opened_page=0)
    result = mapper.audio_to_page(item, book)
    assert result.method == "chapter"
    assert "Chapter 2" in result.chapter_title
    assert 40 <= result.page < 70


def test_no_toc_still_pairs_on_abs_chapter_titles():
    audio = [AudioChapter(f"Chapter {i}", (i - 1) * 100, i * 100) for i in range(1, 6)]
    mapper = PositionMapper(
        chapter_map=True, page_lag=1, audio_lag=15,
        chapter_loader=lambda _id: audio,
        epub_loader=lambda _book: None,
    )
    item = SimpleNamespace(id="abs1", duration=500, current_time=250)
    book = SimpleNamespace(page_count=100, epub_hash="h", title="Book", last_opened_page=0)
    result = mapper.audio_to_page(item, book)
    assert result.method == "chapter"
    assert result.chapter_title == "Chapter 3"


def test_chapter_map_off_is_percent():
    mapper = PositionMapper(chapter_map=False, page_lag=1, audio_lag=15)
    item = SimpleNamespace(id="x", duration=1000, current_time=500)
    book = SimpleNamespace(page_count=200, epub_hash="", title="B", last_opened_page=0)
    result = mapper.audio_to_page(item, book)
    assert result.method == "percent"
