"""Entrypoint: event-driven (ABS Socket.io) with an optional backup poll."""

from __future__ import annotations

import logging
import sys
import time

from .config import Config
from .events import ABSProgressListener, Debouncer, SyncGate
from .mqttpub import start as mqtt_start
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

    use_events = cfg.abs_events and cfg.direction == "audio_to_ebook"
    log.info(
        "paperwhisper starting | direction=%s interval=%ss dry_run=%s "
        "abs_events=%s debounce=%ss max_wait=%ss chapter_map=%s page_lag=%s "
        "audio_lag=%ss user=%s abs=%s",
        cfg.direction, cfg.interval, cfg.dry_run, use_events,
        cfg.event_debounce, cfg.event_max_wait, cfg.chapter_map, cfg.page_lag,
        cfg.audio_lag, cfg.rmfakecloud_user, cfg.abs_url,
    )
    if cfg.dry_run:
        log.info("DRY_RUN is on — no changes will be written. "
                 "Set DRY_RUN=false once you've confirmed the matches look right.")
    if cfg.abs_events and cfg.direction != "audio_to_ebook":
        log.info("ABS_EVENTS is only used for DIRECTION=audio_to_ebook; ignoring")

    if cfg.mqtt_host:
        mqtt_start(cfg)
        log.info("MQTT status -> %s:%s prefix=%s", cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix)

    gate = SyncGate(lambda: run_once(cfg))
    listener = None
    if use_events:
        debouncer = Debouncer(cfg.event_debounce, cfg.event_max_wait, gate.request)
        listener = ABSProgressListener(
            cfg.abs_url, cfg.abs_token,
            on_progress=lambda _item: debouncer.trigger(),
            verify_tls=cfg.abs_verify_tls,
        )
        listener.start()
        log.info(
            "listening for ABS progress events; will write rmfakecloud after %ss quiet "
            "(or %ss of continuous listening). The tablet picks up the page on its next sync.",
            cfg.event_debounce, cfg.event_max_wait,
        )

    if cfg.interval <= 0:
        if listener is None:
            run_once(cfg)
            return 0
        try:
            listener.wait()
        except KeyboardInterrupt:
            listener.stop()
        return 0

    while True:
        try:
            gate.request()
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log.exception("sync pass failed: %s", e)
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
