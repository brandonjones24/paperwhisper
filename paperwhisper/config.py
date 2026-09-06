"""Environment-driven configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DIRECTIONS = {"ebook_to_audio", "audio_to_ebook"}


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

    ebook_provider: str = "remarkable"   # remarkable | calibreweb
    calibre_library: str = ""
    cwa_app_db: str = ""
    cwa_user_id: int = 0

    direction: str = "ebook_to_audio"
    interval: int = 300
    dry_run: bool = True
    match_threshold: float = 0.72
    min_delta: float = 0.01          # ebook_to_audio: min fractional move -> ABS
    min_page_delta: int = 1          # audio_to_ebook: min page move -> reMarkable
    min_progress: float = 0.005      # ignore items barely started
    allow_rewind: bool = False       # if False, only ever advance the target

    state_file: str = "/state/paperwhisper.json"
    log_level: str = "INFO"

    # audio_to_ebook: ABS Socket.io instead of (or in addition to) polling.
    # Default on for audio_to_ebook; ignored for ebook_to_audio.
    abs_events: bool = False
    event_debounce: int = 20         # seconds of quiet listening before writing
    event_max_wait: int = 120        # force a write if events keep arriving

    # Optional: publish sync events to an existing MQTT broker (HA Mosquitto).
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

        self.direction = os.getenv("DIRECTION", "ebook_to_audio")
        self.interval = _int("INTERVAL", 300)
        self.dry_run = _bool("DRY_RUN", True)
        self.match_threshold = _float("MATCH_THRESHOLD", 0.72)
        self.min_delta = _float("MIN_DELTA", 0.01)
        self.min_page_delta = _int("MIN_PAGE_DELTA", 1)
        self.min_progress = _float("MIN_PROGRESS", 0.005)
        self.allow_rewind = _bool("ALLOW_REWIND", False)

        self.state_file = os.getenv("STATE_FILE", "/state/paperwhisper.json")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

        self.event_debounce = _int("EVENT_DEBOUNCE", 20)
        self.event_max_wait = _int("EVENT_MAX_WAIT", 120)
        events_env = os.getenv("ABS_EVENTS")
        if events_env is None:
            self.abs_events = self.direction == "audio_to_ebook"
        else:
            self.abs_events = events_env.strip().lower() in {"1", "true", "yes", "on"}

        self.mqtt_host = os.getenv("MQTT_HOST", "").strip()
        self.mqtt_port = _int("MQTT_PORT", 1883)
        self.mqtt_user = os.getenv("MQTT_USER", "").strip()
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.mqtt_prefix = os.getenv("MQTT_PREFIX", "paperwhisper").strip() or "paperwhisper"
        self.mqtt_discovery = os.getenv("MQTT_DISCOVERY", "homeassistant").strip() or "homeassistant"

    def validate(self) -> list[str]:
        errs = []
        if not self.rmfakecloud_user:
            errs.append("RMFAKECLOUD_USER is required")
        if not self.abs_url:
            errs.append("ABS_URL is required")
        if not self.abs_token:
            errs.append("ABS_TOKEN is required")
        if self.direction not in DIRECTIONS:
            errs.append(f"DIRECTION={self.direction!r} must be one of {sorted(DIRECTIONS)}")
        if self.ebook_provider not in {"remarkable", "calibreweb"}:
            errs.append(f"EBOOK_PROVIDER={self.ebook_provider!r} must be 'remarkable' or 'calibreweb'")
        if self.ebook_provider == "calibreweb":
            if not self.calibre_library:
                errs.append("CALIBRE_LIBRARY is required for the calibreweb provider")
            if not self.cwa_app_db:
                errs.append("CWA_APP_DB is required for the calibreweb provider")
            if self.direction == "audio_to_ebook":
                errs.append("the calibreweb provider only supports DIRECTION=ebook_to_audio (writing back to Calibre-Web is not implemented yet)")
        if self.direction == "audio_to_ebook":
            if not self.rmfakecloud_url:
                errs.append("RMFAKECLOUD_URL is required for audio_to_ebook")
            if not self.rmfakecloud_device_token:
                errs.append(
                    "audio_to_ebook needs a device token: set RMFAKECLOUD_DEVICE_TOKEN "
                    "or mount an rmapi.conf and set RMAPI_CONFIG"
                )
        return errs
