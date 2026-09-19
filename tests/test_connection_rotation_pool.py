"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from exitmgr.connection import IBConnection


def conn(client_id):
    """Public API contract; production-derived narrative omitted."""
    return IBConnection(host="127.0.0.1", port=4001, client_id=client_id)





FIXED_BASES = [
    1,
    189,
    77, 78, 79, 85, 86, 87, 90,
    91,
    92, 93, 94,
    95,
    96, 97, 101,
    170,
    930,
    947,
    972,
    9731,
]
RESERVED_BAND = list(range(110, 150))
EXEC_CAPTURE_BAND = list(range(150, 901))
ALL_BASES = sorted(set(FIXED_BASES + RESERVED_BAND + EXEC_CAPTURE_BAND))


def test_the_hash_identity_that_broke_the_fold_is_real_on_this_interpreter():
    """Public API contract; production-derived narrative omitted."""
    for n in (93, 95, 97, 930, 947, 972, 9731):
        assert hash(n) == n
    assert sys.version_info[:2] >= (3, 12)


@pytest.mark.parametrize("folded,collides_with,shared", [
    (930, 93, [4930, 4931, 4932, 4933]),
    (972, 97, [4972, 4973]),
    (947, 95, [4950]),
    (9731, 173, [5731, 5732, 5733]),
])
def test_the_old_fold_back_collided_and_the_new_one_does_not(folded, collides_with, shared):
    """Public API contract; production-derived narrative omitted."""
    old = 4000 + (abs(hash(folded)) % 4000)
    old_pool = set(range(old, old + IBConnection._ROTATION_POOL_SIZE))
    victim = set(conn(collides_with)._rotation_pool())
    assert old_pool & victim == set(shared), "the documented collision is not reproducible"
    assert not set(conn(folded)._rotation_pool()) & victim


def test_fold_band_sits_above_every_direct_pool_and_every_allocated_id():
    assert (IBConnection._FOLD_FLOOR
            > IBConnection._DIRECT_POOL_CEILING + IBConnection._ROTATION_POOL_SIZE - 1)
    assert IBConnection._FOLD_FLOOR > max(ALL_BASES)

    assert max(conn(499)._rotation_pool()) == 8993
    assert IBConnection._FOLD_FLOOR > 8993
    assert min(conn(500)._rotation_pool()) >= IBConnection._FOLD_FLOOR


def test_every_pool_is_pairwise_disjoint_EXHAUSTIVE():
    """Public API contract; production-derived narrative omitted."""
    owner = {}
    for base in ALL_BASES:
        pool = conn(base)._rotation_pool()
        assert len(pool) == IBConnection._ROTATION_POOL_SIZE
        for cid in pool:
            assert cid not in owner, (
                "clientId %d is in the rotation pool of BOTH base %d and base %d"
                % (cid, owner[cid], base))
            owner[cid] = base
    assert len(owner) == len(ALL_BASES) * IBConnection._ROTATION_POOL_SIZE


def test_no_pool_id_is_itself_an_allocated_client_id_EXHAUSTIVE():
    """Public API contract; production-derived narrative omitted."""
    allocated = set(ALL_BASES) | {112, 116, 117}
    for base in ALL_BASES:
        assert not set(conn(base)._rotation_pool()) & allocated


def test_pool_ids_stay_clear_of_the_reserved_and_random_bands_EXHAUSTIVE():
    """Public API contract; production-derived narrative omitted."""
    forbidden = set(RESERVED_BAND) | set(EXEC_CAPTURE_BAND) | {101}
    for base in ALL_BASES:
        assert not set(conn(base)._rotation_pool()) & forbidden


def test_the_armed_loops_keep_the_pools_production_already_uses():
    """Public API contract; production-derived narrative omitted."""
    assert conn(1)._rotation_pool() == [4010, 4011, 4012, 4013]
    assert conn(189)._rotation_pool() == [5890, 5891, 5892, 5893]


def test_a_non_numeric_client_id_still_yields_a_usable_pool():
    """Public API contract; production-derived narrative omitted."""
    for junk in (None, "", "abc", -5):
        assert conn(junk)._rotation_pool() == [4000, 4001, 4002, 4003]



def test_rotation_index_is_restored_across_a_RESTART(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(tmp_path / "cursor.json"))
    first = conn(1)
    assert first._next_rotation_id() == 4011
    assert first._next_rotation_id() == 4012

    restarted = conn(1)
    assert restarted._rotation_idx is None, "the cursor must be read lazily, not in __init__"
    assert restarted._next_rotation_id() == 4013, (
        "a restarted process re-offered an id its predecessor had already taken")


def test_absent_cursor_never_starts_at_slot_zero(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(tmp_path / "nope.json"))
    c = conn(1)
    assert not os.path.exists(str(tmp_path / "nope.json"))
    assert c._load_rotation_cursor() != 0
    assert c._next_rotation_id() != 4010


@pytest.mark.parametrize("payload", [
    "",
    "{",
    "null",
    "[]",
    '{"next_idx": 2}',
    '{"base": 1}',
    '{"base": 1, "next_idx": "two"}',
    '{"base": 1, "next_idx": -3}',
    '{"base": 999, "next_idx": 2}',
])
def test_corrupt_or_foreign_cursor_degrades_to_the_safe_slot(tmp_path, monkeypatch, payload):
    p = tmp_path / "cursor.json"
    p.write_text(payload)
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(p))
    c = conn(1)
    assert c._load_rotation_cursor() == IBConnection._ROTATION_CURSOR_ABSENT_IDX
    assert c._next_rotation_id() == 4011


def test_a_stale_cursor_is_honoured_not_expired(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    p = tmp_path / "cursor.json"
    p.write_text(json.dumps({"base": 189, "next_idx": 3, "last_id": 5892,
                             "ts": "2019-01-01T00:00:00+00:00"}))
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(p))
    assert conn(189)._next_rotation_id() == 5893


def test_cursor_wraps_and_never_leaves_the_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(tmp_path / "cursor.json"))
    c = conn(189)
    pool = set(c._rotation_pool())
    seen = [c._next_rotation_id() for _ in range(20)]
    assert set(seen) == pool
    assert 0 <= c._rotation_idx < IBConnection._ROTATION_POOL_SIZE


def test_cursor_write_failure_does_not_break_rotation(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH",
                       str(tmp_path / "no-such-dir" / "x" / "cursor.json"))

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "makedirs", boom)
    c = conn(1)
    assert [c._next_rotation_id() for _ in range(3)] == [4011, 4012, 4013]


def test_each_base_gets_its_own_cursor_file(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.delenv("EXITMGR_ROTATION_CURSOR_PATH", raising=False)
    monkeypatch.setenv("EXITMGR_LINK_ROTATIONS_PATH", str(tmp_path / "link-rotations.json"))
    entry, prot = conn(1), conn(189)
    assert entry._ROTATION_CURSOR_PATH != prot._ROTATION_CURSOR_PATH
    assert entry._next_rotation_id() == 4011
    assert prot._next_rotation_id() == 5891
    assert entry._next_rotation_id() == 4012
    for c, expect in ((conn(1), 4013), (conn(189), 5892)):
        assert c._next_rotation_id() == expect


def test_cursor_path_defaults_beside_the_rotation_counter_so_tests_cannot_write_live_state(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.delenv("EXITMGR_ROTATION_CURSOR_PATH", raising=False)
    monkeypatch.setenv("EXITMGR_LINK_ROTATIONS_PATH", str(tmp_path / "link-rotations.json"))
    assert os.path.dirname(conn(1)._ROTATION_CURSOR_PATH) == str(tmp_path)
    conn(1)._next_rotation_id()
    assert (tmp_path / "link-rotation-cursor-1.json").exists()
    assert not (tmp_path / "link-rotations.json").exists(), (
        "the cursor must not be written into the watchdog's counter file")


def test_cursor_file_records_what_a_human_needs(tmp_path, monkeypatch):
    p = tmp_path / "cursor.json"
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(p))
    c = conn(189)
    used = c._next_rotation_id()
    d = json.loads(p.read_text())
    assert d["base"] == 189 and d["last_id"] == used
    assert d["next_idx"] == (1 + IBConnection._ROTATION_CURSOR_ABSENT_IDX) % 4
    assert d["pid"] == os.getpid() and d["ts"]
