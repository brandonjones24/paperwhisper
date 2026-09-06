"""Parse an EPUB's table of contents into fractional positions in the book.

reMarkable reflows EPUBs, so we cannot map TOC hrefs onto tablet page numbers
directly. Instead we weight each spine document by its visible text length
(and, when a TOC href has a fragment id, the offset of that id inside the
file) and return each entry as a 0–1 fraction through the book. Callers then
scale those fractions onto the tablet's ``pageCount``.

Supports EPUB3 ``nav`` and EPUB2 NCX. Malformed files return an empty list
rather than raising.
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree as ET

log = logging.getLogger("paperwhisper.epubtoc")


@dataclass
class TocEntry:
    title: str
    href: str
    start_frac: float  # 0–1 through the book by text weight
    end_frac: float


def parse_epub_toc(data: bytes | None) -> list[TocEntry]:
    """Return TOC entries with start/end fractions, or ``[]`` on failure."""
    if not data:
        return []
    try:
        return _parse_epub_toc(data)
    except Exception as e:  # noqa: BLE001 — TOC is best-effort
        log.warning("epub toc parse failed: %s", e)
        return []


# --------------------------------------------------------------------------- #
# zip / xml helpers                                                           #
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if tag else ""


def _norm_path(path: str) -> str:
    path = (path or "").replace("\\", "/").lstrip("/")
    parts: list[str] = []
    for c in path.split("/"):
        if c in ("", "."):
            continue
        if c == "..":
            if parts:
                parts.pop()
            continue
        parts.append(c)
    return "/".join(parts)


def _split_href(href: str) -> tuple[str, str]:
    path, _, frag = (href or "").partition("#")
    path = path.split("?", 1)[0]
    return _norm_path(unquote(path)), unquote(frag)


def _join(base_dir: str, rel: str) -> str:
    if not rel:
        return _norm_path(base_dir)
    base = base_dir if base_dir.endswith("/") else base_dir + "/"
    return _norm_path(urljoin(base, rel))


class _Zip:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.names = {_norm_path(n): n for n in zf.namelist()}
        self._lower = {k.lower(): v for k, v in self.names.items()}

    def read(self, path: str) -> bytes | None:
        key = _norm_path(path)
        real = self.names.get(key) or self._lower.get(key.lower())
        if real is None:
            return None
        try:
            return self.zf.read(real)
        except KeyError:
            return None


def _parse_xml(data: bytes) -> ET.Element | None:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1, flags=re.I)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        try:
            return ET.fromstring(f"<root>{text}</root>")
        except ET.ParseError:
            return None


# --------------------------------------------------------------------------- #
# HTML text + fragment offsets                                                #
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ids: dict[str, int] = {}
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        d = dict(attrs)
        ident = d.get("id") or (d.get("name") if tag == "a" else None)
        if ident and ident not in self.ids:
            self.ids[ident] = sum(len(p) for p in self.parts)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data:
            self.parts.append(data)


def _html_text_and_ids(data: bytes) -> tuple[str, dict[str, int]]:
    parser = _TextExtractor()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", data.decode("utf-8", errors="replace"))
        return re.sub(r"\s+", " ", text).strip(), {}
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text, parser.ids


# --------------------------------------------------------------------------- #
# container → OPF → spine / TOC                                               #
# --------------------------------------------------------------------------- #
def _parse_epub_toc(data: bytes) -> list[TocEntry]:
    with zipfile.ZipFile(BytesIO(data)) as raw:
        z = _Zip(raw)
        opf_path = _opf_path(z)
        if not opf_path:
            return []
        opf_bytes = z.read(opf_path)
        if not opf_bytes:
            return []
        opf = _parse_xml(opf_bytes)
        if opf is None:
            return []

        opf_dir = posixpath.dirname(opf_path)
        manifest: dict[str, str] = {}
        nav_hrefs: list[str] = []
        ncx_hrefs: list[str] = []
        for item in opf.iter():
            if _local(item.tag) != "item":
                continue
            iid = item.get("id") or ""
            href = item.get("href") or ""
            if not iid or not href:
                continue
            resolved = _join(opf_dir, href)
            manifest[iid] = resolved
            props = (item.get("properties") or "").split()
            media = (item.get("media-type") or "").lower()
            if "nav" in props:
                nav_hrefs.append(resolved)
            if media in {"application/x-dtbncx+xml", "application/x-dtbncxxml"}:
                ncx_hrefs.append(resolved)

        spine: list[str] = []
        seen: set[str] = set()
        for ref in opf.iter():
            if _local(ref.tag) != "itemref":
                continue
            href = manifest.get(ref.get("idref") or "")
            if href and href not in seen:
                spine.append(href)
                seen.add(href)

        toc_hrefs: list[tuple[str, str]] = []
        for nav in nav_hrefs:
            toc_hrefs = _parse_nav(z, nav)
            if toc_hrefs:
                break
        if not toc_hrefs:
            for ncx in ncx_hrefs:
                toc_hrefs = _parse_ncx(z, ncx)
                if toc_hrefs:
                    break
        if len(toc_hrefs) < 2:
            return []

        weights, offsets = _spine_weights(z, spine)
        total = sum(weights) or 1.0
        cum = [0.0]
        running = 0.0
        for w in weights:
            running += w
            cum.append(running)

        path_index = {_norm_path(p): i for i, p in enumerate(spine)}

        entries: list[TocEntry] = []
        for title, href in toc_hrefs:
            path, frag = _split_href(href)
            idx = path_index.get(path)
            if idx is None:
                # try basename match (some TOCs omit directories)
                base = posixpath.basename(path)
                idx = next((i for i, p in enumerate(spine) if posixpath.basename(p) == base), None)
            if idx is None:
                frac = entries[-1].start_frac if entries else 0.0
            else:
                before = cum[idx]
                span = weights[idx]
                inner = 0.0
                if frag:
                    ids = offsets[idx]
                    if frag in ids and span:
                        inner = min(1.0, max(0.0, ids[frag] / max(span, 1)))
                frac = (before + inner * span) / total
            entries.append(TocEntry(title=title, href=href, start_frac=frac, end_frac=1.0))

        return _repair_fracs(entries)


def _opf_path(z: _Zip) -> str | None:
    raw = z.read("META-INF/container.xml")
    if not raw:
        return None
    root = _parse_xml(raw)
    if root is None:
        return None
    for el in root.iter():
        if _local(el.tag) == "rootfile":
            path = el.get("full-path")
            if path:
                return _norm_path(path)
    return None


def _parse_nav(z: _Zip, nav_path: str) -> list[tuple[str, str]]:
    raw = z.read(nav_path)
    if not raw:
        return []
    root = _parse_xml(raw)
    if root is None:
        return []
    nav_el = None
    for el in root.iter():
        if _local(el.tag) != "nav":
            continue
        typ = (
            el.get("{http://www.idpf.org/2007/ops}type")
            or el.get("epub:type")
            or el.get("type")
            or ""
        )
        if "toc" in typ.replace(",", " ").split() or nav_el is None:
            nav_el = el
            if "toc" in typ.replace(",", " ").split():
                break
    if nav_el is None:
        return []
    base = posixpath.dirname(nav_path)
    out: list[tuple[str, str]] = []
    _walk_nav(nav_el, base, out)
    return out


def _walk_nav(el: ET.Element, base: str, out: list[tuple[str, str]]) -> None:
    loc = _local(el.tag)
    if loc == "a":
        href = el.get("href") or ""
        title = " ".join("".join(el.itertext()).split())
        if title:
            out.append((title, _join(base, href) if href else ""))
        return
    for child in list(el):
        _walk_nav(child, base, out)


def _parse_ncx(z: _Zip, ncx_path: str) -> list[tuple[str, str]]:
    raw = z.read(ncx_path)
    if not raw:
        return []
    root = _parse_xml(raw)
    if root is None:
        return []
    base = posixpath.dirname(ncx_path)
    out: list[tuple[str, str]] = []
    _walk_ncx(root, base, out)
    return out


def _walk_ncx(el: ET.Element, base: str, out: list[tuple[str, str]]) -> None:
    if _local(el.tag) == "navPoint":
        title = ""
        href = ""
        for child in list(el):
            loc = _local(child.tag)
            if loc == "navLabel":
                title = " ".join("".join(child.itertext()).split())
            elif loc == "content":
                href = child.get("src") or ""
        if title:
            out.append((title, _join(base, href) if href else ""))
    for child in list(el):
        _walk_ncx(child, base, out)


def _spine_weights(z: _Zip, spine: list[str]) -> tuple[list[float], list[dict[str, int]]]:
    weights: list[float] = []
    offsets: list[dict[str, int]] = []
    for path in spine:
        raw = z.read(path)
        if not raw:
            weights.append(1.0)
            offsets.append({})
            continue
        text, ids = _html_text_and_ids(raw)
        weights.append(float(len(text) or 1))
        offsets.append(ids)
    return weights, offsets


def _repair_fracs(entries: list[TocEntry]) -> list[TocEntry]:
    if not entries:
        return []
    starts = [e.start_frac for e in entries]
    unique = {round(s, 6) for s in starts}
    if len(unique) == 1 and len(entries) > 1:
        n = len(entries)
        return [
            TocEntry(title=e.title, href=e.href, start_frac=i / n, end_frac=(i + 1) / n)
            for i, e in enumerate(entries)
        ]
    # monotonic, end = next start
    last = 0.0
    fixed: list[TocEntry] = []
    for e in entries:
        start = max(0.0, min(1.0, e.start_frac), last)
        fixed.append(TocEntry(title=e.title, href=e.href, start_frac=start, end_frac=1.0))
        last = start
    for i, e in enumerate(fixed):
        end = fixed[i + 1].start_frac if i + 1 < len(fixed) else 1.0
        if end <= e.start_frac:
            end = min(1.0, e.start_frac + 1e-6)
        e.end_frac = end
    fixed[-1].end_frac = 1.0
    return fixed
