"""ABS Socket.io listener + debounce so we write rmfakecloud on listen pauses.

Audiobookshelf has no HTTP webhooks. The web/app clients subscribe to
``user_item_progress_updated`` over Socket.io; we do the same, then run the
existing ``audio_to_ebook`` pass. The tablet is not pushed live — it picks up
the new ``lastOpenedPage`` the next time it syncs (wake / reconnect).

Playback emits progress many times per minute. Writing the rmfakecloud sync
tree on every tick would thrash generation CAS, so events are coalesced:

  * **idle** — fire once listening has been quiet for ``idle_s``
  * **max wait** — fire anyway if events keep arriving for ``max_wait_s``
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

log = logging.getLogger("paperwhisper.events")


class DebouncePolicy:
    """Pure idle + max-wait policy. Times are monotonic seconds."""

    def __init__(self, idle_s: float, max_wait_s: float = 0.0):
        self.idle_s = max(0.0, float(idle_s))
        self.max_wait_s = max(0.0, float(max_wait_s))
        self.pending_since: float | None = None
        self.last_event: float | None = None

    def on_event(self, now: float) -> float:
        """Record an event; return the monotonic time the pass should fire."""
        self.last_event = now
        if self.pending_since is None:
            self.pending_since = now
        idle_at = now + self.idle_s
        if self.max_wait_s > 0:
            return min(idle_at, self.pending_since + self.max_wait_s)
        return idle_at

    def due(self, now: float) -> bool:
        if self.pending_since is None or self.last_event is None:
            return False
        if now >= self.last_event + self.idle_s:
            return True
        if self.max_wait_s > 0 and now >= self.pending_since + self.max_wait_s:
            return True
        return False

    def clear(self) -> None:
        self.pending_since = None
        self.last_event = None


class Debouncer:
    """Thread-safe DebouncePolicy that invokes ``fn`` when due."""

    def __init__(
        self,
        idle_s: float,
        max_wait_s: float,
        fn: Callable[[], None],
        timer_cls=threading.Timer,
        clock=None,
    ):
        self.policy = DebouncePolicy(idle_s, max_wait_s)
        self.fn = fn
        self._timer_cls = timer_cls
        self._clock = clock or __import__("time").monotonic
        self._lock = threading.Lock()
        self._timer = None

    def trigger(self) -> None:
        now = self._clock()
        with self._lock:
            due_at = self.policy.on_event(now)
            delay = max(0.0, due_at - now)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = self._timer_cls(delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            now = self._clock()
            if not self.policy.due(now):
                return
            self.policy.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        try:
            self.fn()
        except Exception:  # noqa: BLE001 — keep the listener alive
            log.exception("debounced sync failed")


class SyncGate:
    """Serialize ``fn`` calls; if one is in flight, run once more after it."""

    def __init__(self, fn: Callable[[], None]):
        self.fn = fn
        self._lock = threading.Lock()
        self._running = False
        self._again = False

    def request(self) -> None:
        with self._lock:
            if self._running:
                self._again = True
                return
            self._running = True
        while True:
            try:
                self.fn()
            except Exception:
                log.exception("sync pass failed")
            with self._lock:
                if self._again:
                    self._again = False
                    continue
                self._running = False
                return


def item_id_from_progress_event(payload: Any) -> str | None:
    """Pull a book libraryItemId out of a Socket.io progress payload.

    Podcast episodes (``episodeId`` set) are ignored — paperwhisper only
    maps audiobooks to ebooks.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    if data.get("episodeId"):
        return None
    return data.get("libraryItemId") or payload.get("id") or None


def _ws_origin(abs_url: str) -> str:
    """Socket.io connects to the origin, not an /api path."""
    parsed = urlparse(abs_url)
    if not parsed.scheme:
        parsed = urlparse("http://" + abs_url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def socket_auth_token(abs_url: str, api_token: str, verify_tls: bool = True) -> str:
    """ABS API keys (JWT ``type=api``) work on REST but Socket.io rejects them.

    POST /api/authorize with the API key and use the returned ``user.token``
    (the user's socket-capable token) for ``emit('auth', ...)``.
    """
    import requests

    url = abs_url.rstrip("/") + "/api/authorize"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=15,
        verify=verify_tls,
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    user = data.get("user") if isinstance(data, dict) else None
    tok = (user or {}).get("token") or (user or {}).get("accessToken")
    if not tok:
        raise RuntimeError("ABS /api/authorize did not return user.token")
    return tok


class ABSProgressListener:
    """Long-lived Socket.io client; calls ``on_progress(item_id)`` per book event."""

    def __init__(self, abs_url: str, token: str, on_progress: Callable[[str], None],
                 verify_tls: bool = True):
        self.origin = _ws_origin(abs_url)
        self.api_token = token
        self.on_progress = on_progress
        self.verify_tls = verify_tls
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket_token: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="abs-socket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait(self) -> None:
        """Block until ``stop()`` (used when there is no backup poll loop)."""
        self._stop.wait()

    def _run(self) -> None:
        try:
            import socketio
        except ImportError:
            log.error("python-socketio is not installed; ABS events disabled")
            return

        sio = socketio.Client(
            reconnection=True,
            reconnection_delay=1,
            reconnection_delay_max=30,
            ssl_verify=self.verify_tls,
            logger=False,
            engineio_logger=False,
        )

        auth_attempts = {"n": 0}

        def _emit_auth():
            if auth_attempts["n"] >= 3:
                log.error("ABS socket auth failed 3 times; waiting for reconnect")
                return
            auth_attempts["n"] += 1
            tok = self._socket_token
            if not tok:
                tok = socket_auth_token(self.origin, self.api_token, self.verify_tls)
                self._socket_token = tok
            sio.emit("auth", tok)

        @sio.event
        def connect():
            auth_attempts["n"] = 0
            log.info("ABS socket connected (%s); authenticating", self.origin)
            try:
                _emit_auth()
            except Exception:
                log.exception("ABS socket auth token exchange failed")

        @sio.event
        def disconnect():
            log.warning("ABS socket disconnected; will reconnect")

        @sio.event
        def connect_error(data):
            log.warning("ABS socket connect_error: %s", data)

        @sio.on("init")
        def on_init(data):
            who = (data or {}).get("username") if isinstance(data, dict) else data
            log.info("ABS socket authenticated as %s", who)

        def _on_auth_fail(data=None):
            log.error("ABS socket rejected token (%s); refreshing", data)
            self._socket_token = None
            try:
                _emit_auth()
            except Exception:
                log.exception("ABS socket re-auth failed")

        @sio.on("invalid_token")
        def on_invalid(data=None):
            _on_auth_fail(data)

        @sio.on("auth_failed")
        def on_auth_failed(data=None):
            _on_auth_fail(data)

        @sio.on("user_item_progress_updated")
        def on_progress(payload):
            item_id = item_id_from_progress_event(payload)
            if not item_id:
                return
            log.info("ABS progress event item=%s", item_id)
            try:
                self.on_progress(item_id)
            except Exception:
                log.exception("on_progress handler failed")

        delay = 2.0
        while not self._stop.is_set():
            try:
                log.info("connecting ABS socket at %s", self.origin)
                sio.connect(
                    self.origin,
                    transports=["websocket"],
                    wait_timeout=15,
                    socketio_path="socket.io",
                )
                delay = 2.0
                while not self._stop.is_set():
                    sio.sleep(1)
            except Exception as e:  # noqa: BLE001
                log.warning("ABS socket error: %s; retry in %.0fs", e, delay)
            finally:
                if sio.connected:
                    try:
                        sio.disconnect()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 60.0)
