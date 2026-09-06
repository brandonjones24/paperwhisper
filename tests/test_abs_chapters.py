"""ABS chapter list normalisation (no HTTP)."""
from paperwhisper.audiobookshelf import chapters_from_audio_files, parse_abs_chapters


def test_fills_missing_end_from_next_start():
    raw = [
        {"title": "Opening Credits", "start": 0},
        {"title": "Chapter 1", "start": 25.5},
        {"title": "Chapter 2", "start": 125.0, "end": 200},
    ]
    ch = parse_abs_chapters(raw, duration=250)
    assert ch[0].end == 25.5
    assert ch[1].end == 125.0
    assert ch[2].end == 250  # stretched to duration


def test_empty_and_junk():
    assert parse_abs_chapters([], 10) == []
    assert parse_abs_chapters(None, 10) == []
    assert parse_abs_chapters(["nope"], 10) == []


def test_audio_file_fallback_needs_two_tracks():
    files = [
        {"metadata": {"title": "Track A"}, "duration": 100},
        {"title": "Track B", "duration": 80, "startOffset": 100},
    ]
    ch = chapters_from_audio_files(files, duration=180)
    assert [c.title for c in ch] == ["Track A", "Track B"]
    assert ch[0].start == 0
    assert ch[1].start == 100
    assert chapters_from_audio_files([files[0]], 100) == []
