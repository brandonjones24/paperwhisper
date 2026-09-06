"""Event debounce + ABS payload parsing. Run: python -m pytest -q tests/test_events.py"""
from paperwhisper.events import (
    DebouncePolicy,
    Debouncer,
    SyncGate,
    _ws_origin,
    item_id_from_progress_event,
)


def test_idle_fires_after_quiet():
    p = DebouncePolicy(idle_s=20, max_wait_s=120)
    due = p.on_event(0)
    assert due == 20
    assert not p.due(19)
    assert p.due(20)


def test_each_event_pushes_idle_out():
    p = DebouncePolicy(idle_s=20, max_wait_s=120)
    p.on_event(0)
    due = p.on_event(15)
    assert due == 35
    assert not p.due(34)
    assert p.due(35)


def test_max_wait_caps_a_long_listen():
    p = DebouncePolicy(idle_s=20, max_wait_s=120)
    p.on_event(0)
    for t in range(10, 130, 10):
        due = p.on_event(t)
    # still receiving events, but pending since 0 so max wait is 120
    assert due == 120
    assert p.due(120)
    assert not p.due(119)


def test_clear_resets():
    p = DebouncePolicy(idle_s=5, max_wait_s=0)
    p.on_event(0)
    p.clear()
    assert not p.due(100)
    due = p.on_event(100)
    assert due == 105


def test_item_id_book_payload():
    payload = {
        "id": "prog-1",
        "data": {"libraryItemId": "li_martian", "currentTime": 100.0, "progress": 0.4},
    }
    assert item_id_from_progress_event(payload) == "li_martian"


def test_item_id_skips_podcast_episodes():
    payload = {
        "id": "prog-2",
        "data": {"libraryItemId": "li_show", "episodeId": "ep_1", "currentTime": 10},
    }
    assert item_id_from_progress_event(payload) is None


def test_item_id_flat_payload():
    assert item_id_from_progress_event({"libraryItemId": "li_x"}) == "li_x"
    assert item_id_from_progress_event("nope") is None


def test_ws_origin_strips_path():
    assert _ws_origin("http://audiobookshelf:13378") == "http://audiobookshelf:13378"
    assert _ws_origin("http://abs.example/audiobookshelf") == "http://abs.example"


def test_debouncer_fires_once_via_fake_timer():
    fired = []

    class FakeTimer:
        last = None

        def __init__(self, delay, fn):
            self.delay = delay
            self.fn = fn
            self.cancelled = False
            FakeTimer.last = self

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            if not self.cancelled:
                self.fn()

    clock = {"t": 0.0}

    d = Debouncer(20, 120, lambda: fired.append(clock["t"]),
                  timer_cls=FakeTimer, clock=lambda: clock["t"])
    d.trigger()
    assert FakeTimer.last.delay == 20
    clock["t"] = 20
    FakeTimer.last.fire()
    assert fired == [20]

    # a second burst should schedule again
    clock["t"] = 25
    d.trigger()
    assert FakeTimer.last.delay == 20


def test_sync_gate_runs_again_if_requested_during_run():
    calls = []
    gate_holder = {}

    def fn():
        calls.append(1)
        if len(calls) == 1:
            gate_holder["g"].request()  # nested request while running

    g = SyncGate(fn)
    gate_holder["g"] = g
    g.request()
    assert calls == [1, 1]
