"""Paper Pro cPages lastOpened is the real reader position."""
from paperwhisper.remarkable import apply_opened_page, opened_page_from_doc


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
