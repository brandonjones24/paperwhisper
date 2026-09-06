"""Mesh sync: every configured backend is an equal peer.

A cluster is the same book on two or more backends. The furthest-ahead copy
is the leader; everyone else is written (with chapter mapping + lag) unless
that would rewind them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .audiobookshelf import AudiobookshelfClient
from .backends import (
    AudiobookshelfBackend,
    CalibreWebBackend,
    RemarkableBackend,
    map_leader_to_target,
    target_delta,
    would_rewind,
)
from .config import Config
from .mapping import PositionMapper
from .mqttpub import emit as mqtt_emit
from .peers import PeerBook, cluster_key, cluster_peers, pick_leader
from .remarkable import RemarkableStore
from .remarkable_writer import RemarkableSyncWriter

log = logging.getLogger("paperwhisper.sync")


class State:
    """Tiny JSON state file so we only write when the source actually moved."""

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            self.data: dict[str, dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict:
        return self.data.get(key, {})

    def record(self, key: str, **fields):
        self.data[key] = fields

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        except OSError as e:
            log.warning("could not persist state to %s: %s", self.path, e)


def get_ebook_provider(cfg: Config):
    """Back-compat helper for tests / older call sites."""
    if cfg.cwa_app_db and cfg.calibre_library:
        from .calibreweb import CalibreWebStore
        return CalibreWebStore(cfg.cwa_app_db, cfg.calibre_library, cfg.cwa_user_id or None)
    return RemarkableStore(cfg.rmfakecloud_data, cfg.rmfakecloud_user)


def build_backends(cfg: Config) -> dict[str, object]:
    out: dict[str, object] = {}
    configured = cfg.configured_backends()

    abs_client = None
    if "audiobookshelf" in configured:
        abs_client = AudiobookshelfClient(cfg.abs_url, cfg.abs_token, verify_tls=cfg.abs_verify_tls)
        if not abs_client.ping():
            log.error("Audiobookshelf not reachable / token invalid")
        else:
            out["audiobookshelf"] = AudiobookshelfBackend(abs_client)

    store = None
    if "remarkable" in configured:
        store = RemarkableStore(cfg.rmfakecloud_data, cfg.rmfakecloud_user)
        writer = None
        if "remarkable" in cfg.writable_backends():
            writer = RemarkableSyncWriter(cfg.rmfakecloud_url, cfg.rmfakecloud_device_token)
        out["remarkable"] = RemarkableBackend(store, writer)

    if "calibreweb" in configured:
        from .calibreweb import CalibreWebStore
        cwa = CalibreWebStore(
            cfg.cwa_app_db, cfg.calibre_library, cfg.cwa_user_id or None,
            base_url=cfg.cwa_url, username=cfg.cwa_user, password=cfg.cwa_password,
            verify_tls=cfg.cwa_verify_tls,
        )
        out["calibreweb"] = CalibreWebBackend(cwa)

    return out


def _epub_loader(backends: dict, cluster: dict[str, PeerBook]):
    def load(_book):
        for name, peer in cluster.items():
            be = backends.get(name)
            getter = getattr(be, "epub_bytes", None)
            if getter is None:
                continue
            try:
                data = getter(peer)
            except Exception as e:  # noqa: BLE001
                log.warning("epub read failed on %s: %s", name, e)
                continue
            if data:
                return data
        return None
    return load


def run_once(cfg: Config, backends: dict | None = None) -> int:
    backends = backends if backends is not None else build_backends(cfg)
    if len(backends) < 2:
        log.error("need at least two reachable backends; have %s", sorted(backends))
        return 0

    abs_be = backends.get("audiobookshelf")
    abs_client = getattr(abs_be, "client", None) if abs_be else None
    mapper = PositionMapper.from_config(cfg, abs_client, store=None)
    mapper._epub_loader = None  # set per cluster

    groups: dict[str, list[PeerBook]] = {}
    for name, be in backends.items():
        try:
            groups[name] = list(be.books())
        except Exception as e:  # noqa: BLE001
            log.error("failed to list %s: %s", name, e)
            groups[name] = []
        log.info("%s: %d book(s)", name, len(groups[name]))

    clusters = cluster_peers(groups, cfg.match_threshold)
    targets = set(backends)
    if cfg.direction == "ebook_to_audio":
        targets &= {"audiobookshelf"}
    elif cfg.direction == "audio_to_ebook":
        targets -= {"audiobookshelf"}
    targets = {n for n in targets if getattr(backends[n], "writable", False)}
    state = State(cfg.state_file)
    updates = 0

    log.info("mesh: %d cluster(s) | targets=%s", len(clusters), sorted(targets))

    for cluster in clusters:
        if cfg.direction == "ebook_to_audio":
            leader = pick_leader(
                {k: v for k, v in cluster.items() if k != "audiobookshelf"},
                cfg.min_progress,
            )
        elif cfg.direction == "audio_to_ebook":
            leader = cluster.get("audiobookshelf")
            if leader is None or leader.progress < cfg.min_progress:
                continue
        else:
            leader = pick_leader(cluster, cfg.min_progress)
        if leader is None:
            continue

        key = cluster_key(cluster)
        frac = round(leader.progress, 4)
        if state.get(key).get("leader_frac") == frac and state.get(key).get("leader") == f"{leader.backend}:{leader.ident}":
            continue

        mapper._epub_loader = _epub_loader(backends, cluster)
        mapper._toc_cache = {}

        wrote_any = False
        for name, peer in cluster.items():
            if name == leader.backend:
                continue
            if name not in targets:
                continue
            be = backends.get(name)
            if be is None or not getattr(be, "writable", False):
                continue
            mapped = map_leader_to_target(mapper, leader, peer, cluster)
            delta = target_delta(peer, mapped)
            min_delta = (cfg.min_page_delta / peer.page_count) if (name == "remarkable" and peer.page_count) else cfg.min_delta
            log.info(
                "MATCH %s %r -> %s %r (leader %.1f%%) [%s]",
                leader.backend, leader.title, name, peer.title,
                leader.progress * 100, mapped.describe(),
            )
            if not cfg.allow_rewind and would_rewind(peer, mapped):
                log.debug("skip: would rewind %s %r", name, peer.title)
                continue
            if delta < min_delta:
                continue
            if cfg.dry_run:
                dest = mapped.page if mapped.page is not None else mapped.seconds if mapped.seconds is not None else mapped.frac
                log.info("[DRY_RUN] would set %s %r to %s", name, peer.title, dest)
                updates += 1
                wrote_any = True
                continue
            try:
                if be.apply(peer, mapped):
                    updates += 1
                    wrote_any = True
            except Exception as e:  # noqa: BLE001
                log.error("failed to update %s %r: %s", name, peer.title, e)

        if wrote_any:
            mqtt_emit({
                "title": leader.title,
                "progress_pct": round(leader.progress * 100, 1),
                "updates": updates,
                "dry_run": cfg.dry_run,
                "detail": f"{leader.backend} {leader.progress:.1%} -> {', '.join(sorted(cluster))}",
            })
        state.record(key, leader=f"{leader.backend}:{leader.ident}", leader_frac=frac)

    state.save()
    log.info("pass complete: %d update(s)%s", updates, " (dry-run)" if cfg.dry_run else "")
    return updates
