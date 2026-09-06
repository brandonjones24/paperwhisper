"""Equal-weight clustering: furthest-ahead leader, no primary backend."""
from paperwhisper.peers import PeerBook, cluster_key, cluster_peers, pick_leader


def _book(backend, title, progress, ident=None, author="Andy Weir"):
    return PeerBook(
        backend=backend,
        ident=ident or f"{backend}-{title[:8]}",
        title=title,
        author=author,
        progress=progress,
        updated_ms=int(progress * 1000),
        page_count=100 if backend == "remarkable" else 0,
        last_opened_page=int(progress * 100) if backend == "remarkable" else 0,
        duration=1000 if backend == "audiobookshelf" else 0,
        current_time=progress * 1000 if backend == "audiobookshelf" else 0,
    )


def test_cluster_three_backends_same_book():
    groups = {
        "audiobookshelf": [_book("audiobookshelf", "The Martian", 0.40)],
        "remarkable": [_book("remarkable", "The Martian: A Novel", 0.55)],
        "calibreweb": [_book("calibreweb", "The Martian", 0.20)],
    }
    clusters = cluster_peers(groups, 0.72)
    assert len(clusters) == 1
    c = clusters[0]
    assert set(c) == {"audiobookshelf", "remarkable", "calibreweb"}
    leader = pick_leader(c, 0.005)
    assert leader.backend == "remarkable"
    assert leader.progress == 0.55


def test_does_not_cross_match_different_titles():
    groups = {
        "audiobookshelf": [
            _book("audiobookshelf", "Harry Potter and the Prisoner of Azkaban", 0.5),
        ],
        "remarkable": [
            _book("remarkable", "Harry Potter and the Sorcerer's Stone", 0.5),
        ],
    }
    assert cluster_peers(groups, 0.72) == []


def test_leader_tie_breaks_on_timestamp():
    a = _book("audiobookshelf", "Artemis", 0.5, ident="a")
    a.updated_ms = 10
    b = _book("remarkable", "Artemis", 0.5, ident="b")
    b.updated_ms = 20
    leader = pick_leader({"audiobookshelf": a, "remarkable": b}, 0.0)
    assert leader.ident == "b"


def test_cluster_key_stable():
    c = {
        "remarkable": _book("remarkable", "X", 0.1, ident="r"),
        "audiobookshelf": _book("audiobookshelf", "X", 0.1, ident="a"),
    }
    assert cluster_key(c) == "audiobookshelf:a|remarkable:r"
