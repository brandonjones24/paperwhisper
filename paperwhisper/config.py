"""Environment-driven configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DIRECTIONS = {"all", "ebook_to_audio", "audio_to_ebook"}


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _read_device_token() -> str:
    """Device token for writing to rmfakecloud (audio_to_ebook).

    Prefer RMFAKECLOUD_DEVICE_TOKEN; otherwise parse it out of an rmapi.conf
    pointed at by RMAPI_CONFIG (the file rmapi writes after registering)."""
    tok = os.getenv("RMFAKECLOUD_DEVICE_TOKEN", "").strip()
    if tok:
        return tok
    conf = os.getenv("RMAPI_CONFIG", "").strip()
    if conf and os.path.exists(conf):
        try:
            m = re.search(r"devicetoken:\s*(\S+)", open(conf).read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return ""


@dataclass
class Config:
    # Fields are populated from the environment in __post_init__ so a Config()
    # always reflects the current environment (not import-time values).
    rmfakecloud_data: str = ""
    rmfakecloud_user: str = ""
    rmfakecloud_url: str = ""
    rmfakecloud_device_token: str = ""

    abs_url: str = ""
    abs_token: str = ""
    abs_verify_tls: bool = True

    ebook_provider: str = "remarkable"   # deprecated: backends enable themselves
    calibre_library: str = ""
    cwa_app_db: str = ""
    cwa_user_id: int = 0
    cwa_url: str = ""
    cwa_user: str = ""
    cwa_password: str = ""
    cwa_verify_tls: bool = True

    direction: str = "all"
    interval: int = 300
    dry_run: bool = True
    match_threshold: float = 0.72
    min_delta: float = 0.01          # ebook_to_audio: min fractional move -> ABS
    min_page_delta: int = 1          # audio_to_ebook: min page move -> reMarkable
    min_progress: float = 0.005      # ignore items barely started
    allow_rewind: bool = False       # if False, only ever advance the target
    chapter_map: bool = True         # ABS chapters ↔ EPUB TOC when they pair
    page_lag: int = 1                # land this many pages behind the mapped page
    audio_lag: float = 15.0          # land this many seconds behind the mapped time

    state_file: str = "/state/paperwhisper.json"
    log_level: str = "INFO"

    # audio_to_ebook: ABS Socket.io instead of (or in addition to) polling.
    # Default on for audio_to_ebook; ignored for ebook_to_audio.
    abs_events: bool = False
    event_debounce: int = 20         # seconds of quiet listening before writing
    event_max_wait: int = 120        # force a write if events keep arriving

    # Optional MQTT status. Disabled unless MQTT_HOST is set.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "paperwhisper"
    mqtt_discovery: str = "homeassistant"

    def __post_init__(self):
        self.rmfakecloud_data = os.getenv("RMFAKECLOUD_DATA", "/rmdata")
        self.rmfakecloud_user = os.getenv("RMFAKECLOUD_USER", "")
        self.rmfakecloud_url = os.getenv("RMFAKECLOUD_URL", "")
        self.rmfakecloud_device_token = _read_device_token()

        self.abs_url = os.getenv("ABS_URL", "")
        self.abs_token = os.getenv("ABS_TOKEN", "")
        self.abs_verify_tls = _bool("ABS_VERIFY_TLS", True)

        self.ebook_provider = os.getenv("EBOOK_PROVIDER", "remarkable").lower()
        self.calibre_library = os.getenv("CALIBRE_LIBRARY", "")
        self.cwa_app_db = os.getenv("CWA_APP_DB", "")
        self.cwa_user_id = _int("CWA_USER_ID", 0)
        self.cwa_url = os.getenv("CWA_URL", "").strip()
        self.cwa_user = os.getenv("CWA_USER", "").strip()
        self.cwa_password = os.getenv("CWA_PASSWORD", "")
        self.cwa_verify_tls = _bool("CWA_VERIFY_TLS", True)

        self.direction = os.getenv("DIRECTION", "all")
        self.interval = _int("INTERVAL", 300)
        self.dry_run = _bool("DRY_RUN", True)
        self.match_threshold = _float("MATCH_THRESHOLD", 0.72)
        self.min_delta = _float("MIN_DELTA", 0.01)
        self.min_page_delta = _int("MIN_PAGE_DELTA", 1)
        self.min_progress = _float("MIN_PROGRESS", 0.005)
        self.allow_rewind = _bool("ALLOW_REWIND", False)
        self.chapter_map = _bool("CHAPTER_MAP", True)
        self.page_lag = max(0, _int("PAGE_LAG", 1))
        self.audio_lag = max(0.0, _float("AUDIO_LAG", 15))

        self.state_file = os.getenv("STATE_FILE", "/state/paperwhisper.json")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

        self.event_debounce = _int("EVENT_DEBOUNCE", 20)
        self.event_max_wait = _int("EVENT_MAX_WAIT", 120)
        events_env = os.getenv("ABS_EVENTS")
        if events_env is None:
            self.abs_events = self.direction in {"audio_to_ebook", "all"}
        else:
            self.abs_events = events_env.strip().lower() in {"1", "true", "yes", "on"}

        self.mqtt_host = os.getenv("MQTT_HOST", "").strip()
        self.mqtt_port = _int("MQTT_PORT", 1883)
        self.mqtt_user = os.getenv("MQTT_USER", "").strip()
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.mqtt_prefix = os.getenv("MQTT_PREFIX", "paperwhisper").strip() or "paperwhisper"
        self.mqtt_discovery = os.getenv("MQTT_DISCOVERY", "homeassistant").strip() or "homeassistant"

    def configured_backends(self) -> list[str]:
        """Backends with enough config to *read* progress."""
        out: list[str] = []
        if self.abs_url and self.abs_token:
            out.append("audiobookshelf")
        if self.rmfakecloud_user:
            out.append("remarkable")
        if self.cwa_app_db and self.calibre_library:
            out.append("calibreweb")
        return out

    def writable_backends(self) -> list[str]:
        out: list[str] = []
        if self.abs_url and self.abs_token:
            out.append("audiobookshelf")
        if self.rmfakecloud_user and self.rmfakecloud_url and self.rmfakecloud_device_token:
            out.append("remarkable")
        if self.cwa_app_db and self.calibre_library and self.cwa_url and self.cwa_user and self.cwa_password:
            out.append("calibreweb")
        return out

    def source_backends(self) -> list[str]:
        configured = self.configured_backends()
        if self.direction == "ebook_to_audio":
            return [b for b in configured if b != "audiobookshelf"]
        if self.direction == "audio_to_ebook":
            return [b for b in configured if b == "audiobookshelf"]
        return configured

    def target_backends(self) -> list[str]:
        writable = self.writable_backends()
        if self.direction == "ebook_to_audio":
            return [b for b in writable if b == "audiobookshelf"]
        if self.direction == "audio_to_ebook":
            return [b for b in writable if b != "audiobookshelf"]
        return writable

    def validate(self) -> list[str]:
        errs = []
        if self.direction not in DIRECTIONS:
            errs.append(f"DIRECTION={self.direction!r} must be one of {sorted(DIRECTIONS)}")
        backends = self.configured_backends()
        if len(backends) < 2:
            errs.append(
                "configure at least two backends (Audiobookshelf, reMarkable, Calibre-Web)"
            )
        if "calibreweb" in backends:
            if not self.calibre_library:
                errs.append("CALIBRE_LIBRARY is required for Calibre-Web")
            if not self.cwa_app_db:
                errs.append("CWA_APP_DB is required for Calibre-Web")
        targets = self.target_backends()
        if self.direction == "audio_to_ebook" and not targets:
            errs.append(
                "audio_to_ebook needs a writable ebook backend: reMarkable "
                "(RMFAKECLOUD_URL + device token) and/or Calibre-Web (CWA_URL + CWA_USER + CWA_PASSWORD)"
            )
        if self.direction == "ebook_to_audio" and "audiobookshelf" not in targets:
            errs.append("ebook_to_audio needs ABS_URL and ABS_TOKEN")
        return errs
