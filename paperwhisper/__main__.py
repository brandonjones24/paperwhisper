"""Entrypoint: run the sync on an interval (or once)."""

from __future__ import annotations

import logging
import sys
import time

from .config import Config
from .sync import run_once


def main() -> int:
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("paperwhisper")

    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error("config error: %s", e)
        return 2

    log.info(
        "paperwhisper starting | direction=%s interval=%ss dry_run=%s user=%s abs=%s",
        cfg.direction, cfg.interval, cfg.dry_run, cfg.rmfakecloud_user, cfg.abs_url,
    )
    if cfg.dry_run:
        log.info("DRY_RUN is on — no changes will be written to Audiobookshelf. "
                 "Set DRY_RUN=false once you've confirmed the matches look right.")

    if cfg.interval <= 0:
        run_once(cfg)
        return 0

    while True:
        try:
            run_once(cfg)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log.exception("sync pass failed: %s", e)
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
