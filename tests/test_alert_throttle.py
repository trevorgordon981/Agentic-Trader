"""Public API contract; production-derived narrative omitted."""
import json
import os

import pytest

from exitmgr import alerting
from exitmgr import alert_throttle as T





_REAL_POST = alerting.post

H = 3600.0
FID = ("Fidelity export sync is blocked on the Mac\n~/Downloads is unreadable to the "
       "launchd agent (macOS TCC).")


def page(i):
    """Public API contract; production-derived narrative omitted."""
    return ("POSITION MAY BE UNPROTECTED - a close intent cannot be resolved\n"
            "* SYMD con_id=5001002\n"
            "* placed 2026-08-22T11:%02d:%02d.658141+00:00 (age %d min), qty 1\n"
            "* clean passes %d/5" % (10 + i // 6, i, 50 + i, i % 6))


@pytest.fixture
def st(tmp_path):
    return str(tmp_path / "throttle.json")


def test_a_brand_new_condition_pages_immediately(st):
    go, _, _ = T.consider("something just broke", "C1", now=0.0, state_path=st)
    assert go, "a first-occurrence alert must never be delayed"


def test_the_SYMD_storm_collapses_to_a_single_page(st):
    sends = sum(T.consider(page(i), "C1", now=i * 2.2, state_path=st)[0] for i in range(40))
    assert sends == 1, "40 pages for one con_id in 90s must collapse to 1, got %d" % sends


def test_a_different_position_still_pages_during_a_storm(st):
    for i in range(40):
        T.consider(page(i), "C1", now=i * 2.2, state_path=st)
    go, _, _ = T.consider("POSITION MAY BE UNPROTECTED - cannot resolve\n"
                          "* AAPL con_id=111222333\n* (age 2 min), qty 4",
                          "C1", now=45.0, state_path=st)
    assert go, "a NEW con_id must page instantly even mid-storm -- this is the whole point"


def test_a_different_headline_about_the_same_position_still_pages(st):
    T.consider(page(0), "C1", now=0.0, state_path=st)
    go, _, _ = T.consider("UNTRANSMITTED CLOSE INTENT RELEASED - re-arming\n"
                          "* SYMD con_id=5001002\n* (age 67 min)",
                          "C1", now=5.0, state_path=st)
    assert go, "a different KIND of event on the same con_id is different news"


def test_repetition_decays_but_never_goes_silent(st):
    """Public API contract; production-derived narrative omitted."""
    times = [0.43, 0.50, 0.50, 0.98, 1.98, 5.02, 6.02, 7.90, 8.98, 10.47,
             12.48, 13.48, 14.97, 17.37, 20.45, 21.68, 24.40]
    sends = [h for h in times if T.consider(FID, "C1", now=h * H, state_path=st)[0]]
    assert 3 <= len(sends) <= 6, "expected a handful from 17, got %d" % len(sends)
    assert sends[0] == times[0], "the FIRST report must be immediate"
    assert len(sends) > 0, "a persistent condition must never fall fully silent"


def test_backoff_grows_rather_than_using_one_flat_window(st):
    """Public API contract; production-derived narrative omitted."""
    t, sends = 0.0, []
    while t < 60 * H:
        if T.consider(FID, "C1", now=t, state_path=st)[0]:
            sends.append(t)
        t += 60.0
    gaps = [b - a for a, b in zip(sends, sends[1:])]
    assert gaps == sorted(gaps), "backoff must be non-decreasing, got %s" % gaps
    assert gaps[-1] >= 8 * H, "a long-stuck condition should settle to >=8h, got %.1fh" % (gaps[-1] / H)


def test_held_messages_are_counted_and_surfaced_when_the_key_next_fires(st):
    T.consider(FID, "C1", now=0.0, state_path=st)
    for i in range(1, 30):
        T.consider(FID, "C1", now=i * 60.0, state_path=st)
    go, held, note = T.consider(FID, "C1", now=1.5 * H, state_path=st)
    assert go and held == 29, "expected 29 held, got go=%s held=%s" % (go, held)
    assert "29 identical alerts suppressed" in note
    assert "still unfixed" in note, "the volume itself should read as evidence"


def test_a_cleared_condition_is_forgotten_so_it_pages_instantly_next_time(st):
    for i in range(6):
        T.consider(FID, "C1", now=i * 13 * H, state_path=st)
    go, _, _ = T.consider(FID, "C1", now=(5 * 13 * H) + T.FORGET_AFTER + 60, state_path=st)
    assert go, "after a long quiet gap the condition is new again and must page at once"


def test_it_fails_open_when_the_state_file_is_corrupt(tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json at all")
    go, _, _ = T.consider(FID, "C1", now=0.0, state_path=str(bad))
    assert go, "a corrupt state file must SEND, never suppress"


def test_it_fails_open_when_the_state_path_is_unwritable(tmp_path):
    blocked = tmp_path / "afile"
    blocked.write_text("x")
    go, _, _ = T.consider(FID, "C1", now=0.0, state_path=str(blocked / "sub" / "s.json"))
    assert go, "an unwritable state path must SEND"


def test_an_unkeyable_message_always_sends(st):
    for i in range(5):
        go, _, _ = T.consider(None, "C1", now=i * 60.0, state_path=st)
        assert go, "a message we cannot key is a message we must not suppress"


def test_throttled_post_reports_success_not_failure(monkeypatch, st):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(alerting, "post", _REAL_POST)
    monkeypatch.setattr(T, "STATE", st)
    calls = []
    monkeypatch.setattr(alerting, "api", lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(alerting, "token", lambda: "x")
    assert alerting.post(FID, "C1", label="t") is True
    assert alerting.post(FID, "C1", label="t") is True, "throttled call must return True"
    assert len(calls) == 1, "the second call must not have reached Slack"


def test_dedup_false_always_sends(monkeypatch, st):
    monkeypatch.setattr(alerting, "post", _REAL_POST)
    monkeypatch.setattr(T, "STATE", st)
    calls = []
    monkeypatch.setattr(alerting, "api", lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(alerting, "token", lambda: "x")
    for _ in range(4):
        alerting.post(FID, "C1", dedup=False)
    assert len(calls) == 4, "dedup=False must post every time"


def test_a_broken_throttle_does_not_silence_an_alert(monkeypatch, st):
    monkeypatch.setattr(alerting, "post", _REAL_POST)

    def explode(*a, **k):
        raise RuntimeError("throttle is broken")

    monkeypatch.setattr(T, "consider", explode)
    calls = []
    monkeypatch.setattr(alerting, "api", lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(alerting, "token", lambda: "x")
    assert alerting.post("live money page", "C1") is True
    assert len(calls) == 1, "a throttle exception must fall through to SENDING"


def test_trade_approvals_do_not_route_through_the_throttled_post():
    """Public API contract; production-derived narrative omitted."""
    import inspect
    from exitmgr import approval
    src = inspect.getsource(approval)
    assert "alerting.post" not in src, (
        "approval.py must not post through the throttled path -- an approval prompt that "
        "gets deduped is a trade an operator is never asked about")
