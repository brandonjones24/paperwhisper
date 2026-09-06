"""RemarkableStore exposes the .epub blob for TOC parsing."""
from paperwhisper.remarkable import RemarkableBook, RemarkableStore


def test_epub_bytes_reads_blob(tmp_path):
    sync = tmp_path / "users" / "me" / "sync"
    sync.mkdir(parents=True)
    (sync / "deadbeef").write_bytes(b"PK\x03\x04epub")
    store = RemarkableStore(tmp_path, "me")
    book = RemarkableBook(uuid="u", title="T", epub_hash="deadbeef")
    assert store.epub_bytes(book) == b"PK\x03\x04epub"
    assert store.epub_bytes(RemarkableBook(uuid="u", title="T")) is None
