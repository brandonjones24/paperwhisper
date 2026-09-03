"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


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


@dataclass
class Config:
    # rmfakecloud (read-only)
    rmfakecloud_data: str = os.getenv("RMFAKECLOUD_DATA", "/rmdata")
    rmfakecloud_user: str = os.getenv("RMFAKECLOUD_USER", "")

    # Audiobookshelf
    abs_url: str = os.getenv("ABS_URL", "")
    abs_token: str = os.getenv("ABS_TOKEN", "")
    abs_verify_tls: bool = _bool("ABS_VERIFY_TLS", True)

    # behaviour
    direction: str = os.getenv("DIRECTION", "ebook_to_audio")
    interval: int = _int("INTERVAL", 300)        # seconds; 0 = run once
    dry_run: bool = _bool("DRY_RUN", True)        # safe by default
    match_threshold: float = _float("MATCH_THRESHOLD", 0.72)
    # Only push an update when the ebook position moves the audiobook by at least
    # this fraction, to avoid churn / tiny jitters.
    min_delta: float = _float("MIN_DELTA", 0.01)
    # Ignore the front/back matter mismatch by clamping; if the ebook is <this,
    # treat as "not really started" and skip.
    min_progress: float = _float("MIN_PROGRESS", 0.005)

    state_file: str = os.getenv("STATE_FILE", "/state/paperwhisper.json")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self) -> list[str]:
        errs = []
        if not self.rmfakecloud_user:
            errs.append("RMFAKECLOUD_USER is required")
        if not self.abs_url:
            errs.append("ABS_URL is required")
        if not self.abs_token:
            errs.append("ABS_TOKEN is required")
        if self.direction not in {"ebook_to_audio"}:
            errs.append(
                f"DIRECTION={self.direction!r} not supported yet "
                "(only 'ebook_to_audio'; audio_to_ebook is experimental/unimplemented)"
            )
        return errs
