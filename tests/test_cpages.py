"""Paper Pro cPages lastOpened is the real reader position."""
from paperwhisper.remarkable import _derive_page_count, apply_opened_page, opened_page_from_doc


def _martian_content(open_id, n=508):
    pages = [{"id": f"id-{i}", "redir": {"value": i}} for i in range(n)]
    return {
        "fileType": "epub",
        "lastOpenedPage": 327,
        "pageCount": n,
        "cPages": {
            "lastOpened": {"timestamp": "1:11", "value": open_id},
            "pages": pages,
        },
    }


def test_reads_cpages_uuid_not_integer_field():
    content = _martian_content("id-199")
    meta = {"lastOpenedPage": 327}
    assert opened_page_from_doc(meta, content) == 199


def test_falls_back_to_lastOpenedPage_without_cpages():
    meta = {"lastOpenedPage": 160}
    content = {"lastOpenedPage": 160, "pageCount": 198, "pages": ["x"] * 198}
    assert opened_page_from_doc(meta, content) == 160


def test_apply_sets_cpages_uuid_and_bumps_timestamp():
    content = _martian_content("id-199")
    assert apply_opened_page(content, 327) is True
    assert content["lastOpenedPage"] == 327
    lo = content["cPages"]["lastOpened"]
    assert lo["value"] == "id-327"
    assert lo["timestamp"] == "1:12"
    assert apply_opened_page(content, 327) is False


def _content_with_tombstones(real_n=354, tomb_n=222, lastOpened_id="id-0"):
    """Real doc shape: N live pages (redir == own index) followed by
    deleted tombstone entries whose redir scatters back into 0..N-1, and
    whose *array position* can collide with a real page number -- e.g.
    tomb-0 sits at raw index `real_n` but redir'd back to page 0."""
    real = [{"id": f"id-{i}", "redir": {"value": i}} for i in range(real_n)]
    tomb = [
        {"id": f"tomb-{i}", "redir": {"value": i % real_n}, "deleted": {"value": 1}}
        for i in range(tomb_n)
    ]
    return {
        "fileType": "epub",
        "lastOpenedPage": 0,
        "pageCount": real_n,
        "cPages": {
            "original": {"value": real_n},
            "lastOpened": {"timestamp": "1:11", "value": lastOpened_id},
            "pages": real + tomb,
        },
    }


def test_page_count_prefers_pageCount_over_inflated_cpages_length():
    """The real bug: cPages.pages had 576 entries (354 real + 222 deleted
    tombstones) but pageCount said 354. page_count must report 354, not the
    inflated 576 you'd get from a raw len(cPages.pages)."""
    content = _content_with_tombstones()
    assert len(content["cPages"]["pages"]) == 354 + 222
    assert _derive_page_count(content) == 354


def test_page_count_falls_back_to_cpages_original_without_pageCount():
    content = _content_with_tombstones()
    del content["pageCount"]
    assert _derive_page_count(content) == 354


def test_page_count_counts_only_live_entries_as_last_resort():
    content = _content_with_tombstones()
    del content["pageCount"]
    del content["cPages"]["original"]
    assert _derive_page_count(content) == 354


def test_apply_targets_live_entry_via_redir_not_raw_tombstone_index():
    """Real page 300 as a raw array index would land on a tombstone (index
    300 is a live real page here, but tomb-0..N sit right after index 354,
    so a raw index of e.g. 360 would hit tomb-6, not real page 360 -- which
    doesn't even exist). Writing real page 300 must resolve to the live
    entry, never a deleted one, even though a deleted entry with the same
    redir could appear earlier in the list."""
    content = _content_with_tombstones()
    # Put a *deleted* entry with redir==300 before the real page-300 entry,
    # to prove the live one is preferred rather than whichever is found first.
    content["cPages"]["pages"].insert(0, {"id": "decoy-300", "redir": {"value": 300}, "deleted": {"value": 1}})
    assert apply_opened_page(content, 300) is True
    lo = content["cPages"]["lastOpened"]
    assert lo["value"] == "id-300"


def test_opened_page_from_doc_follows_redir_when_lastOpened_is_a_tombstone():
    """Defensive: if lastOpened ever points at a deleted entry, resolve via
    its redir (the real page it represents) rather than its raw array slot."""
    content = _content_with_tombstones(lastOpened_id="tomb-5")
    # tomb-5 -> redir value 5 % 354 == 5
    assert opened_page_from_doc({}, content) == 5

