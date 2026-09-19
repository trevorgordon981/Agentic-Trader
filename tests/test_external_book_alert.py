import json

import cross_book
from exitmgr import risk


def _stale():
    return risk.ExternalBook.stale(
        "external book data is 80.0h old (max 72.0h)",
        age_s=80 * 3600.0,
        max_age_s=72 * 3600.0,
        n_positions=3,
    )


def _ok():
    return risk.ExternalBook.known(
        {"SYMZ": 190.0},
        age_s=1.0,
        max_age_s=72 * 3600.0,
    )


def test_fault_posts_once_and_recovery_posts_once(tmp_path):
    state = tmp_path / "state.json"
    sent = []
    post = lambda text: sent.append(text) or True

    assert cross_book.notify_external_book(
        _stale(), state_path=str(state), poster=post, host="bat")[0]
    assert len(sent) == 1 and "STALE" in sent[0]
    assert json.loads(state.read_text())["status"] == "STALE"

    assert cross_book.notify_external_book(
        _stale(), state_path=str(state), poster=post, host="bat")[0]
    assert len(sent) == 1

    assert cross_book.notify_external_book(
        _ok(), state_path=str(state), poster=post, host="bat")[0]
    assert len(sent) == 2 and "current again" in sent[1]
    assert json.loads(state.read_text())["status"] == "OK"

    assert cross_book.notify_external_book(
        _ok(), state_path=str(state), poster=post, host="bat")[0]
    assert len(sent) == 2


def test_failed_delivery_does_not_suppress_retry(tmp_path):
    state = tmp_path / "state.json"
    sent = []

    ok, posted = cross_book.notify_external_book(
        _stale(), state_path=str(state),
        poster=lambda text: sent.append(text) or False, host="bat")
    assert not ok and posted and not state.exists()

    ok, posted = cross_book.notify_external_book(
        _stale(), state_path=str(state),
        poster=lambda text: sent.append(text) or True, host="bat")
    assert ok and posted and len(sent) == 2
    assert json.loads(state.read_text())["status"] == "STALE"


def test_initial_healthy_state_is_silent(tmp_path):
    state = tmp_path / "state.json"
    sent = []
    ok, posted = cross_book.notify_external_book(
        _ok(), state_path=str(state),
        poster=lambda text: sent.append(text) or True, host="bat")
    assert ok and not posted and sent == []
    assert json.loads(state.read_text())["status"] == "OK"
