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


def test_abs_events_explicit_false():
    _clear_event_env()
    os.environ["DIRECTION"] = "audio_to_ebook"
    os.environ["ABS_EVENTS"] = "false"
    cfg = Config()
    assert cfg.abs_events is False
