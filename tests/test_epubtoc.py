"""EPUB TOC parser: nav + NCX, text-weighted fractions."""
from __future__ import annotations

import zipfile
from io import BytesIO

from paperwhisper.epubtoc import parse_epub_toc


def _epub(*, nav: str | None, ncx: str | None, files: dict[str, str], spine: list[str]) -> bytes:
    """Minimal EPUB2/3 zip. ``files`` maps OEBPS-relative path → HTML."""
    manifest_items = []
    if nav:
        manifest_items.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
    if ncx:
        manifest_items.append(
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        )
    for i, name in enumerate(spine):
        manifest_items.append(
            f'<item id="s{i}" href="{name}" media-type="application/xhtml+xml"/>'
        )
    spine_refs = "".join(f'<itemref idref="s{i}"/>' for i in range(len(spine)))
    toc_attr = ' toc="ncx"' if ncx else ""
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">test</dc:identifier>
    <dc:title>Test</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine{toc_attr}>
    {spine_refs}
  </spine>
</package>
"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        if nav:
            zf.writestr("OEBPS/nav.xhtml", nav)
        if ncx:
            zf.writestr("OEBPS/toc.ncx", ncx)
        for name, html in files.items():
            zf.writestr(f"OEBPS/{name}", html)
    return buf.getvalue()


def _nav(entries: list[tuple[str, str]]) -> str:
    items = "".join(f'<li><a href="{href}">{title}</a></li>' for title, href in entries)
    return f"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body>
<nav epub:type="toc"><ol>{items}</ol></nav>
</body></html>
"""


def _ncx(entries: list[tuple[str, str]]) -> str:
    points = []
    for i, (title, href) in enumerate(entries, 1):
        points.append(
            f'<navPoint id="n{i}" playOrder="{i}">'
            f"<navLabel><text>{title}</text></navLabel>"
            f'<content src="{href}"/>'
            f"</navPoint>"
        )
    return f"""<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>{"".join(points)}</navMap>
</ncx>
"""


def _html(text: str, frag_id: str | None = None) -> str:
    extra = f'<h1 id="{frag_id}">{frag_id}</h1>' if frag_id else ""
    return f"<html><body>{extra}<p>{text}</p></body></html>"


def test_nav_text_weighted_fractions():
    # ch1 is ~4× the text of ch2, so chapter 2 should start near 0.8
    files = {
        "ch1.xhtml": _html("alpha " * 400),
        "ch2.xhtml": _html("beta " * 100),
    }
    data = _epub(
        nav=_nav([("Chapter 1", "ch1.xhtml"), ("Chapter 2", "ch2.xhtml")]),
        ncx=None,
        files=files,
        spine=["ch1.xhtml", "ch2.xhtml"],
    )
    toc = parse_epub_toc(data)
    assert [e.title for e in toc] == ["Chapter 1", "Chapter 2"]
    assert toc[0].start_frac == 0.0
    assert 0.75 < toc[1].start_frac < 0.85
    assert toc[-1].end_frac == 1.0


def test_ncx_when_no_nav():
    files = {
        "a.xhtml": _html("aaaa " * 50),
        "b.xhtml": _html("bbbb " * 50),
    }
    data = _epub(
        nav=None,
        ncx=_ncx([("One", "a.xhtml"), ("Two", "b.xhtml")]),
        files=files,
        spine=["a.xhtml", "b.xhtml"],
    )
    toc = parse_epub_toc(data)
    assert [e.title for e in toc] == ["One", "Two"]
    assert toc[1].start_frac == 0.5


def test_nested_nav_flattens():
    nav = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol>
  <li><a href="part.xhtml">Part 1</a>
    <ol>
      <li><a href="ch1.xhtml">Chapter 1</a></li>
      <li><a href="ch2.xhtml">Chapter 2</a></li>
    </ol>
  </li>
</ol></nav></body></html>
"""
    files = {
        "part.xhtml": _html("part " * 10),
        "ch1.xhtml": _html("one " * 10),
        "ch2.xhtml": _html("two " * 10),
    }
    data = _epub(nav=nav, ncx=None, files=files, spine=["part.xhtml", "ch1.xhtml", "ch2.xhtml"])
    toc = parse_epub_toc(data)
    assert [e.title for e in toc] == ["Part 1", "Chapter 1", "Chapter 2"]


def test_fragment_id_splits_a_spine_item():
    html = "<html><body><p>" + "AAAA " * 100 + '</p><h1 id="ch2">ch2</h1><p>' + "BBBB " * 100 + "</p></body></html>"
    files = {"all.xhtml": html}
    nav = _nav([("Chapter 1", "all.xhtml"), ("Chapter 2", "all.xhtml#ch2")])
    data = _epub(nav=nav, ncx=None, files=files, spine=["all.xhtml"])
    toc = parse_epub_toc(data)
    assert toc[0].start_frac == 0.0
    assert toc[1].start_frac > 0.2  # not collapsed to the same position


def test_garbage_and_empty():
    assert parse_epub_toc(None) == []
    assert parse_epub_toc(b"not a zip") == []
    assert parse_epub_toc(b"PK\x03\x04notreally") == []
