"""Equal-weight matching across backends.

A *cluster* is the same book as it appears on two or more backends
(reMarkable, Audiobookshelf, Calibre-Web). The leader is whoever is furthest
ahead; everyone else catches up. No backend is primary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .matcher import best_match


@dataclass
class PeerBook:
    backend: str
    ident: str
    title: str
    author: str
    progress: float  # 0–1
    updated_ms: int = 0
    page_count: int = 0
    last_opened_page: int = 0
    duration: float = 0.0
    current_time: float = 0.0
    epub_hash: str = ""
    extra: dict = field(default_factory=dict)


def cluster_peers(
    groups: dict[str, list[PeerBook]],
    threshold: float,
) -> list[dict[str, PeerBook]]:
    """Greedy clusters: each maps backend name -> one PeerBook. Len >= 2."""
    used: dict[str, set[str]] = {name: set() for name in groups}
    seeds = sorted(
        (b for books in groups.values() for b in books),
        key=lambda b: (-b.progress, -b.updated_ms, b.backend, b.ident),
    )
    clusters: list[dict[str, PeerBook]] = []
    for seed in seeds:
        if seed.ident in used[seed.backend]:
            continue
        cluster: dict[str, PeerBook] = {seed.backend: seed}
        used[seed.backend].add(seed.ident)
        for other_name, books in groups.items():
            if other_name in cluster:
                continue
            available = [b for b in books if b.ident not in used[other_name]]
            match, _ = best_match(
                seed.title, seed.author, available,
                key_title=lambda b: b.title, key_author=lambda b: b.author,
                threshold=threshold,
            )
            if match:
                cluster[other_name] = match
                used[other_name].add(match.ident)
        if len(cluster) >= 2:
            clusters.append(cluster)
        else:
            used[seed.backend].discard(seed.ident)
    return clusters


def pick_leader(cluster: dict[str, PeerBook], min_progress: float) -> PeerBook | None:
    """Furthest ahead wins; timestamp then backend name break ties."""
    candidates = [b for b in cluster.values() if b.progress >= min_progress]
    if not candidates:
        return None
    return max(candidates, key=lambda b: (b.progress, b.updated_ms, b.backend))


def cluster_key(cluster: dict[str, PeerBook]) -> str:
    parts = [f"{b.backend}:{b.ident}" for b in sorted(cluster.values(), key=lambda x: x.backend)]
    return "|".join(parts)
