"""Config defaults for ABS events. Run: python -m pytest -q tests/test_config.py"""
import os

from paperwhisper.config import Config


def _clear_event_env():
    for k in ("ABS_EVENTS", "EVENT_DEBOUNCE", "EVENT_MAX_WAIT", "DIRECTION",
              "RMFAKECLOUD_USER", "ABS_URL", "ABS_TOKEN"):
        os.environ.pop(k, None)


def test_abs_events_defaults_on_for_audio_to_ebook():
    _clear_event_env()
    os.environ["DIRECTION"] = "audio_to_ebook"
    cfg = Config()
    assert cfg.abs_events is True
    assert cfg.event_debounce == 20
    assert cfg.event_max_wait == 120


def test_abs_events_defaults_off_for_ebook_to_audio():
    _clear_event_env()
    os.environ["DIRECTION"] = "ebook_to_audio"
    cfg = Config()
    assert cfg.abs_events is False


def test_direction_all_is_default():
    _clear_event_env()
    cfg = Config()
    assert cfg.direction == "all"
    assert cfg.abs_events is True


def test_abs_events_explicit_false():
    _clear_event_env()
    os.environ["DIRECTION"] = "audio_to_ebook"
    os.environ["ABS_EVENTS"] = "false"
    cfg = Config()
    assert cfg.abs_events is False


def test_chapter_map_defaults():
    for k in ("CHAPTER_MAP", "PAGE_LAG", "AUDIO_LAG"):
        os.environ.pop(k, None)
    cfg = Config()
    assert cfg.chapter_map is True
    assert cfg.page_lag == 1
    assert cfg.audio_lag == 15


def test_configured_backends_need_two():
    _clear_event_env()
    os.environ.pop("RMFAKECLOUD_USER", None)
    os.environ.pop("ABS_URL", None)
    os.environ.pop("ABS_TOKEN", None)
    os.environ.pop("CWA_APP_DB", None)
    os.environ.pop("CALIBRE_LIBRARY", None)
    cfg = Config()
    assert cfg.configured_backends() == []
    errs = cfg.validate()
    assert any("at least two backends" in e for e in errs)


def test_chapter_map_env():
    os.environ["CHAPTER_MAP"] = "false"
    os.environ["PAGE_LAG"] = "2"
    os.environ["AUDIO_LAG"] = "30"
    cfg = Config()
    assert cfg.chapter_map is False
    assert cfg.page_lag == 2
    assert cfg.audio_lag == 30
    for k in ("CHAPTER_MAP", "PAGE_LAG", "AUDIO_LAG"):
        os.environ.pop(k, None)
