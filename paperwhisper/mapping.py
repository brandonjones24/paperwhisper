"""Map audiobook time ↔ ebook page.

When Audiobookshelf chapters and the EPUB table of contents can be paired,
progress is interpolated *inside* the matched chapter so you land in the
right chapter rather than at ``round(percent × pages)``. Pairing prefers
chapter numbers, then fuzzy titles, then equal-count index alignment.

If they don't pair cleanly we fall back to a percentage, always with a small
conservative lag (``floor``, then ``PAGE_LAG`` pages / ``AUDIO_LAG`` seconds)
so the target is slightly behind the source — you turn the page (or skip)
forward, not back.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable

from .epubtoc import TocEntry, parse_epub_toc

log = logging.getLogger("paperwhisper.mapping")

MIN_PAIRS = 3
MIN_COVERAGE = 0.5
CHAPTER_TITLE_THRESHOLD = 0.72

# Packaging / credits that exist on one side and not the other.
_SKIP_EXACT = {
    "opening credits", "end credits", "closing credits", "credits",
    "title", "title page", "cover", "copyright", "copyright page",
    "dedication", "contents", "table of contents", "toc",
    "acknowledgments", "acknowledgements", "about the author",
    "also by", "also by this author", "other books", "other titles",
    "front matter", "back matter", "colophon", "imprint",
    "list of characters", "cast", "dramatis personae", "isbn",
}
_SKIP_PREFIX = (
    "praise for", "also by", "copyright", "about the", "opening credit",
    "end credit", "closing credit", "excerpt from", "preview of",
    "a note from", "note from the",
)

_ONES = (
    "one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_WORD_TO_NUM: dict[str, int] = {w: i + 1 for i, w in enumerate(_ONES)}
_WORD_TO_NUM.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50})
for _tens, _tv in (("twenty", 20), ("thirty", 30), ("forty", 40)):
    for _i, _w in enumerate(_ONES[:9], 1):
        _WORD_TO_NUM[f"{_tens} {_w}"] = _tv + _i
        _WORD_TO_NUM[f"{_tens}{_w}"] = _tv + _i

_ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20, "xxi": 21,
    "xxii": 22, "xxiii": 23, "xxiv": 24, "xxv": 25, "xxvi": 26, "xxx": 30,
    "xl": 40, "l": 50,
}

_CH_NUM = re.compile(
    r"\b(?:chapter|ch\.?)\s*(\d+)\b",
    re.I,
)
_CH_WORD = re.compile(
    r"\b(?:chapter|ch\.?)\s+([a-z]+(?:[\s-][a-z]+)?)\b",
    re.I,
)
_CH_ROMAN = re.compile(
    r"\b(?:chapter|ch\.?)\s+(xiv|xiii|xii|xi|ix|viii|vii|vi|iv|iii|ii|xix|xx|xvi|xv|xvii|xviii|xxi|xxii|xxiii|xxiv|xxv|xxvi|xxx|xl|x|v|i)\b",
    re.I,
)
_LEAD_NUM = re.compile(r"^\s*(\d+)\s*[\.\:\)]\s+")
_NUMWORD_RE = "|".join(re.escape(w) for w in sorted(_WORD_TO_NUM, key=len, reverse=True))
_CORE_PREFIX = re.compile(
    rf"^(?:chapter|ch\.?|pt\.?|part)\s+(?:\d+|{_NUMWORD_RE}|[ivxl]+)\b\s*",
    re.I,
)


@dataclass
class AudioChapter:
    title: str
    start: float  # seconds
    end: float


@dataclass
class EbookChapter:
    title: str
    start_page: int
    end_page: int  # exclusive


@dataclass
class AlignedChapter:
    title: str
    audio_start: float
    audio_end: float
    page_start: int
    page_end: int  # exclusive


@dataclass
class MappingResult:
    page: int | None = None
    seconds: float | None = None
    frac: float | None = None  # 0–1, for percentage targets (Calibre-Web)
    method: str = "percent"  # "chapter" | "percent"
    chapter_title: str = ""
    chapter_frac: float = 0.0
    reason: str = ""

    def describe(self) -> str:
        if self.method == "chapter" and self.chapter_title:
            return f"ch {self.chapter_title!r} {self.chapter_frac:.0%}"
        return self.method


# --------------------------------------------------------------------------- #
# title / number helpers                                                      #
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    text = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def chapter_number(title: str) -> int | None:
    """Best-effort chapter number, or None if the title isn't numbered."""
    if not title:
        return None
    m = _CH_NUM.search(title)
    if m:
        return int(m.group(1))
    m = _CH_ROMAN.search(title)
    if m:
        return _ROMAN.get(m.group(1).lower())
    m = _CH_WORD.search(title)
    if m:
        word = m.group(1).lower().replace("-", " ")
        if word in _WORD_TO_NUM:
            return _WORD_TO_NUM[word]
        if word in _ROMAN:
            return _ROMAN[word]
    m = _LEAD_NUM.match(title)
    if m:
        return int(m.group(1))
    n = _norm(title)
    if n in _WORD_TO_NUM:
        return _WORD_TO_NUM[n]
    if n in _ROMAN:
        return _ROMAN[n]
    return None


def title_core(title: str) -> str:
    """Title with a leading 'Chapter N' / '1.' prefix stripped."""
    t = title or ""
    t = _LEAD_NUM.sub("", t, count=1)
    t = _CORE_PREFIX.sub("", t, count=1)
    return _norm(t)


def is_skippable(title: str) -> bool:
    n = _norm(title)
    if not n:
        return True
    if n in _SKIP_EXACT:
        return True
    return any(n.startswith(p) for p in _SKIP_PREFIX)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return (seq + jac) / 2


def chapter_score(a: str, b: str) -> float:
    """0–1 similarity. Different chapter numbers never match."""
    na, nb = chapter_number(a), chapter_number(b)
    if na is not None and nb is not None and na != nb:
        return 0.0
    ca, cb = title_core(a), title_core(b)
    core = _ratio(ca, cb) if ca and cb else 0.0
    if na is not None and na == nb:
        # Number identity is enough ("Chapter 5" ↔ "Chapter 5: SOL").
        return max(0.9, core)
    if ca and cb:
        return core
    return _ratio(_norm(a), _norm(b))


# --------------------------------------------------------------------------- #
# alignment                                                                   #
# --------------------------------------------------------------------------- #
def _body_count(chapters) -> int:
    n = sum(1 for c in chapters if not is_skippable(c.title))
    return n or len(chapters)


def _alignment_ok(pairs: list[tuple[int, int]], audio, ebook) -> bool:
    if not pairs:
        return False
    n = max(_body_count(audio), _body_count(ebook))
    if n <= 2:
        return len(pairs) >= min(n, len(audio), len(ebook)) and len(pairs) >= 1
    return len(pairs) >= MIN_PAIRS and (len(pairs) / n) >= MIN_COVERAGE


def _numbered_pairs(audio, ebook) -> list[tuple[int, int]]:
    a_map: dict[int, int] = {}
    for i, ch in enumerate(audio):
        n = chapter_number(ch.title)
        if n is not None and n not in a_map:
            a_map[n] = i
    e_map: dict[int, int] = {}
    for j, ch in enumerate(ebook):
        n = chapter_number(ch.title)
        if n is not None and n not in e_map:
            e_map[n] = j
    return [(a_map[n], e_map[n]) for n in sorted(set(a_map) & set(e_map))]


def _sequential_pairs(audio, ebook, threshold: float = CHAPTER_TITLE_THRESHOLD) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    j0 = 0
    for i, ach in enumerate(audio):
        best_j, best_s = None, 0.0
        for j in range(j0, len(ebook)):
            s = chapter_score(ach.title, ebook[j].title)
            if s > best_s:
                best_s, best_j = s, j
            if s >= 0.99:
                break
        if best_j is not None and best_s >= threshold:
            pairs.append((i, best_j))
            j0 = best_j + 1
    return pairs


def _index_pairs(audio, ebook) -> list[tuple[int, int]]:
    """Last-resort: same number of body chapters, in the same order."""
    a = [(i, c) for i, c in enumerate(audio) if not is_skippable(c.title)]
    e = [(i, c) for i, c in enumerate(ebook) if not is_skippable(c.title)]
    if len(a) != len(e) or len(a) < MIN_PAIRS:
        return []
    pairs = [(ia, ie) for (ia, _), (ie, _) in zip(a, e)]
    agree = sum(
        1 for ia, ie in pairs if chapter_score(audio[ia].title, ebook[ie].title) >= 0.5
    )
    if agree < max(1, len(pairs) // 3):
        return []
    return pairs


def _materialize(pairs: list[tuple[int, int]], audio, ebook) -> list[AlignedChapter]:
    out: list[AlignedChapter] = []
    for ia, ie in pairs:
        a, e = audio[ia], ebook[ie]
        title = e.title or a.title
        out.append(
            AlignedChapter(
                title=title,
                audio_start=a.start,
                audio_end=max(a.end, a.start),
                page_start=e.start_page,
                page_end=max(e.end_page, e.start_page + 1),
            )
        )
    return out


def align_chapters(audio: list[AudioChapter], ebook: list[EbookChapter]) -> list[AlignedChapter] | None:
    if not audio or not ebook:
        return None
    for builder in (_numbered_pairs, _sequential_pairs, _index_pairs):
        pairs = builder(audio, ebook)
        if _alignment_ok(pairs, audio, ebook):
            return _materialize(pairs, audio, ebook)
    return None


def toc_to_ebook_chapters(toc: list[TocEntry], page_count: int) -> list[EbookChapter]:
    if not toc or page_count <= 0:
        return []
    out: list[EbookChapter] = []
    for i, e in enumerate(toc):
        start = int(e.start_frac * page_count)
        end = int(e.end_frac * page_count) if i + 1 < len(toc) else page_count
        start = max(0, min(start, page_count - 1))
        end = max(start + 1, min(end, page_count))
        if out:
            start = max(start, out[-1].end_page)
            if start >= page_count:
                break
            end = max(start + 1, min(end, page_count))
            out[-1].end_page = min(out[-1].end_page, start)
        out.append(EbookChapter(title=e.title, start_page=start, end_page=end))
    if out:
        out[-1].end_page = page_count
    return out


def duration_weighted_ebook_chapters(
    audio: list[AudioChapter], page_count: int, duration: float
) -> list[EbookChapter]:
    """Split the ebook by audiobook chapter duration when there is no TOC."""
    if not audio or page_count <= 0:
        return []
    total = duration or max(c.end for c in audio) or 1.0
    out: list[EbookChapter] = []
    for ch in audio:
        start = int((ch.start / total) * page_count)
        end = int((ch.end / total) * page_count)
        start = max(0, min(start, page_count - 1))
        end = max(start + 1, min(end, page_count))
        if out:
            start = max(start, out[-1].end_page)
            if start >= page_count:
                break
            end = max(start + 1, min(end, page_count))
        out.append(EbookChapter(title=ch.title, start_page=start, end_page=end))
    if out:
        out[-1].end_page = page_count
    return out


# --------------------------------------------------------------------------- #
# interpolation + lag                                                         #
# --------------------------------------------------------------------------- #
def percent_to_page(progress: float, page_count: int, page_lag: int = 1) -> int:
    if page_count <= 0:
        return 0
    page = math.floor(max(0.0, min(1.0, progress)) * page_count)
    page = min(page, page_count - 1)
    return max(0, page - max(0, page_lag))


def percent_to_seconds(progress: float, duration: float, audio_lag: float = 15.0) -> float:
    if duration <= 0:
        return 0.0
    t = max(0.0, min(1.0, progress)) * duration - max(0.0, audio_lag)
    return max(0.0, min(t, duration))


def _frac_in(pos: float, start: float, end: float) -> float:
    span = end - start
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (pos - start) / span))


def _pair_at_time(pairs: list[AlignedChapter], t: float) -> AlignedChapter | None:
    hit = None
    for p in pairs:
        if t >= p.audio_start:
            hit = p
        else:
            break
    return hit


def _pair_at_page(pairs: list[AlignedChapter], page: int) -> AlignedChapter | None:
    hit = None
    for p in pairs:
        if page >= p.page_start:
            hit = p
        else:
            break
    return hit


def map_time_to_page(
    time: float,
    duration: float,
    page_count: int,
    pairs: list[AlignedChapter] | None,
    page_lag: int = 1,
    audio_lag: float = 15.0,
) -> MappingResult:
    page_lag = max(0, int(page_lag))
    audio_lag = max(0.0, float(audio_lag))
    t = max(0.0, time - audio_lag)
    if page_count <= 0:
        return MappingResult(page=0, method="percent", reason="no pages")

    if pairs:
        hit = _pair_at_time(pairs, t)
        if hit is None:
            page = max(0, pairs[0].page_start - 1 - page_lag)
            return MappingResult(
                page=page, method="chapter", chapter_title="(front matter)",
                chapter_frac=0.0, reason="before first chapter",
            )
        frac = _frac_in(t, hit.audio_start, hit.audio_end)
        span = max(1, hit.page_end - hit.page_start)
        raw = hit.page_start + math.floor(frac * span)
        raw = min(raw, hit.page_end - 1, page_count - 1)
        raw = max(hit.page_start, raw - page_lag)
        return MappingResult(
            page=max(0, raw), method="chapter",
            chapter_title=hit.title, chapter_frac=frac,
        )

    page = percent_to_page((t / duration) if duration else 0.0, page_count, page_lag=page_lag)
    return MappingResult(page=page, method="percent", reason="no chapter alignment")


def map_page_to_seconds(
    page: int,
    page_count: int,
    duration: float,
    pairs: list[AlignedChapter] | None,
    page_lag: int = 1,
    audio_lag: float = 15.0,
) -> MappingResult:
    page_lag = max(0, int(page_lag))
    audio_lag = max(0.0, float(audio_lag))
    if duration <= 0:
        return MappingResult(seconds=0.0, method="percent", reason="no duration")

    if pairs and page_count > 0:
        hit = _pair_at_page(pairs, page)
        if hit is None:
            return MappingResult(
                seconds=0.0, method="chapter", chapter_title="(front matter)",
                chapter_frac=0.0, reason="before first chapter",
            )
        frac = _frac_in(page, hit.page_start, hit.page_end)
        span = max(0.0, hit.audio_end - hit.audio_start)
        t = hit.audio_start + frac * span
        t = max(hit.audio_start, t - audio_lag)
        return MappingResult(
            seconds=t, method="chapter",
            chapter_title=hit.title, chapter_frac=frac,
        )

    progress = (page / page_count) if page_count else 0.0
    t = percent_to_seconds(progress, duration, audio_lag=audio_lag)
    return MappingResult(seconds=t, method="percent", reason="no chapter alignment")


# --------------------------------------------------------------------------- #
# high-level mapper (I/O + cache)                                             #
# --------------------------------------------------------------------------- #
class PositionMapper:
    """Fetches ABS chapters + EPUB TOC, aligns them, maps a position.

    Loaders are callables so tests don't need a live store or HTTP client.
    """

    def __init__(
        self,
        *,
        chapter_map: bool = True,
        page_lag: int = 1,
        audio_lag: float = 15.0,
        chapter_loader: Callable[[str], list[AudioChapter]] | None = None,
        epub_loader: Callable | None = None,
    ):
        self.chapter_map = chapter_map
        self.page_lag = page_lag
        self.audio_lag = audio_lag
        self._chapter_loader = chapter_loader
        self._epub_loader = epub_loader
        self._toc_cache: dict[str, list[TocEntry]] = {}

    @classmethod
    def from_config(cls, cfg, abs_client, store=None) -> "PositionMapper":
        def chapters(item_id: str) -> list[AudioChapter]:
            if not cfg.chapter_map or abs_client is None:
                return []
            try:
                raw = abs_client.chapters(item_id) or []
            except Exception as e:  # noqa: BLE001
                log.warning("ABS chapters failed for %s: %s", item_id, e)
                return []
            out: list[AudioChapter] = []
            for c in raw:
                out.append(
                    c if isinstance(c, AudioChapter)
                    else AudioChapter(title=c.title, start=float(c.start), end=float(c.end))
                )
            return out

        def epub(book):
            if store is None:
                return None
            getter = getattr(store, "epub_bytes", None)
            if getter is None:
                return None
            try:
                return getter(book)
            except Exception as e:  # noqa: BLE001
                log.warning("epub read failed for %s: %s", getattr(book, "title", "?"), e)
                return None

        return cls(
            chapter_map=cfg.chapter_map,
            page_lag=cfg.page_lag,
            audio_lag=cfg.audio_lag,
            chapter_loader=chapters,
            epub_loader=epub,
        )

    def _audio_chapters(self, item) -> list[AudioChapter]:
        if not self.chapter_map or self._chapter_loader is None:
            return []
        ident = getattr(item, "id", None) or ""
        if not ident:
            return []
        return self._chapter_loader(ident) or []

    def _ebook_chapters(self, book, audio: list[AudioChapter], duration: float) -> list[EbookChapter]:
        page_count = int(getattr(book, "page_count", 0) or 0)
        if page_count <= 0:
            return []
        toc = self._toc(book)
        if toc:
            return toc_to_ebook_chapters(toc, page_count)
        if audio:
            return duration_weighted_ebook_chapters(audio, page_count, duration)
        return []

    def _toc(self, book) -> list[TocEntry]:
        if not self.chapter_map or self._epub_loader is None:
            return []
        key = getattr(book, "epub_hash", None) or getattr(book, "uuid", None) or ""
        if key and key in self._toc_cache:
            return self._toc_cache[key]
        data = self._epub_loader(book)
        toc = parse_epub_toc(data)
        if key:
            self._toc_cache[key] = toc
        return toc

    def _pairs(self, item, book) -> tuple[list[AlignedChapter] | None, str]:
        duration = float(getattr(item, "duration", 0) or 0)
        audio = self._audio_chapters(item)
        if not audio:
            return None, "no abs chapters"
        ebook = self._ebook_chapters(book, audio, duration)
        if not ebook:
            return None, "no ebook chapters"
        aligned = align_chapters(audio, ebook)
        if not aligned:
            return None, "chapters did not align"
        return aligned, "aligned"

    def audio_to_page(self, item, book) -> MappingResult:
        page_count = int(getattr(book, "page_count", 0) or 0)
        duration = float(getattr(item, "duration", 0) or 0)
        time = float(getattr(item, "current_time", 0) or 0)
        pairs, reason = self._pairs(item, book)
        result = map_time_to_page(
            time, duration, page_count, pairs,
            page_lag=self.page_lag, audio_lag=self.audio_lag,
        )
        if result.method != "chapter":
            result.reason = result.reason or reason
            log.debug("percent map for %r: %s", getattr(book, "title", "?"), result.reason)
        return result

    def page_to_seconds(self, book, item) -> MappingResult:
        page_count = int(getattr(book, "page_count", 0) or 0)
        duration = float(getattr(item, "duration", 0) or 0)
        page = int(getattr(book, "last_opened_page", 0) or 0)
        pairs, reason = self._pairs(item, book)
        result = map_page_to_seconds(
            page, page_count, duration, pairs,
            page_lag=self.page_lag, audio_lag=self.audio_lag,
        )
        if result.method != "chapter":
            result.reason = result.reason or reason
            log.debug("percent map for %r: %s", getattr(book, "title", "?"), result.reason)
        return result

    def progress_to_seconds(self, progress: float, duration: float) -> MappingResult:
        t = percent_to_seconds(progress, duration, audio_lag=self.audio_lag)
        frac = (t / duration) if duration else 0.0
        return MappingResult(seconds=t, frac=frac, method="percent")
