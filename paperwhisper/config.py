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

    def __post_init__(self):
        self.rmfakecloud_data = os.getenv("RMFAKECLOUD_DATA", "/rmdata")
        self.rmfakecloud_user = os.getenv("RMFAKECLOUD_USER", "")
        self.rmfakecloud_url = os.getenv("RMFAKECLOUD_URL", "")
        self.rmfakecloud_device_token = _read_device_token()

        self.abs_url = os.getenv("ABS_URL", "")
        self.abs_token = os.getenv("ABS_TOKEN", "")
        self.abs_verify_tls = _bool("ABS_VERIFY_TLS", True)

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
        if self.direction == "audio_to_ebook":
            if not self.rmfakecloud_url:
                errs.append("RMFAKECLOUD_URL is required for audio_to_ebook")
            if not self.rmfakecloud_device_token:
                errs.append(
                    "audio_to_ebook needs a device token: set RMFAKECLOUD_DEVICE_TOKEN "
                    "or mount an rmapi.conf and set RMAPI_CONFIG"
                )
        return errs
