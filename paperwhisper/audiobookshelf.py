"""Minimal Audiobookshelf API client (read + write listening progress).

Only the handful of endpoints paperwhisper needs, using a user API token
(Settings -> Users -> your user -> API Token).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

log = logging.getLogger("paperwhisper.abs")


@dataclass
class ABSItem:
    id: str
    title: str
    author: str
    duration: float          # seconds
    current_time: float = 0.0  # seconds listened
    progress: float = 0.0      # 0.0-1.0
    is_finished: bool = False


class AudiobookshelfClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: int = 20):
        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.session.verify = verify_tls

    def _get(self, path: str, **params):
        r = self.session.get(urljoin(self.base, path), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict):
        r = self.session.patch(urljoin(self.base, path), json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r

    # -- reads -----------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._get("api/libraries")
            return True
        except requests.RequestException as e:
            log.error("Audiobookshelf auth/connection failed: %s", e)
            return False

    def _library_ids(self, media_type: str = "book") -> list[str]:
        data = self._get("api/libraries")
        libs = data.get("libraries", data if isinstance(data, list) else [])
        return [l["id"] for l in libs if l.get("mediaType", "book") == media_type]

    def _my_progress(self) -> dict[str, dict]:
        """Map libraryItemId -> progress record for the current user."""
        me = self._get("api/me")
        return {p["libraryItemId"]: p for p in me.get("mediaProgress", [])}

    def audiobooks(self) -> list[ABSItem]:
        """All book-library items with their current listening progress."""
        progress = self._my_progress()
        items: list[ABSItem] = []
        for lib_id in self._library_ids("book"):
            page = 0
            while True:
                data = self._get(f"api/libraries/{lib_id}/items", limit=200, page=page)
                results = data.get("results", [])
                if not results:
                    break
                for it in results:
                    media = it.get("media", {}) or {}
                    md = media.get("metadata", {}) or {}
                    pr = progress.get(it["id"], {})
                    items.append(
                        ABSItem(
                            id=it["id"],
                            title=(md.get("title") or "").strip(),
                            author=(md.get("authorName") or "").strip(),
                            duration=float(media.get("duration") or 0.0),
                            current_time=float(pr.get("currentTime") or 0.0),
                            progress=float(pr.get("progress") or 0.0),
                            is_finished=bool(pr.get("isFinished")),
                        )
                    )
                if len(results) < 200:
                    break
                page += 1
        return items

    # -- writes ----------------------------------------------------------------

    def set_progress(self, item_id: str, current_time: float, duration: float) -> None:
        """Set listening position (seconds) for a library item."""
        progress = max(0.0, min(1.0, current_time / duration)) if duration else 0.0
        payload = {
            "currentTime": round(current_time, 3),
            "duration": round(duration, 3),
            "progress": round(progress, 5),
            "isFinished": progress >= 0.99,
        }
        self._patch(f"api/me/progress/{item_id}", payload)
        log.info("ABS progress set: item=%s -> %.1fs (%.1f%%)", item_id, current_time, progress * 100)
