"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.pnl_semantics import ledger_net, net_realized


def test_a_stored_net_is_trusted():
    net, basis = net_realized({"realized_pnl": -23.0, "realized_pnl_net": -27.18})
    assert net == -27.18 and basis == "stored_net"


@pytest.mark.parametrize("marker", [
    {"usable_for_pnl": False},
    {"pnl_valid": False},
    {"pnl_quarantined": True},
    {"close": {"pnl_valid": False}},
    {"close": {"pnl_quarantined": True}},
])
def test_an_explicit_invalid_marker_outranks_a_numeric_stored_net(marker):
    row = {"realized_pnl": -23.0, "realized_pnl_net": -27.18, **marker}
    assert net_realized(row) == (None, "invalid")


@pytest.mark.parametrize("shape", [
    {"commission_unknown": True, "realized_pnl_net": 91.0},
    {"commission_unknown": True, "realized_pnl": 100.0,
     "entry_commission": 4.0, "exit_commission": 5.0},
    {"close": {"commission_unknown": True}, "realized_pnl_net": 91.0},
])
def test_commission_unknown_outranks_numeric_fields(shape):
    assert net_realized(shape) == (None, "invalid")


def test_gross_with_both_fees_is_netted():
    net, basis = net_realized({"realized_pnl": -23.0,
                               "entry_commission": 2.0869, "exit_commission": 2.0903})
    assert net == -27.18 and basis == "gross_minus_fees"


@pytest.mark.parametrize("row", [
    {"realized_pnl": -23.0, "entry_commission": 2.09},
    {"realized_pnl": -23.0, "exit_commission": 2.09},
    {"realized_pnl": -23.0},
])
def test_a_row_without_both_fees_is_unknown_not_gross(row):
    net, basis = net_realized(row)
    assert net is None and basis == "unknown", (
        "treating an unfee'd row as net understates the loss -- report it instead")


def test_the_discredited_heuristic_stays_dead():
    """Public API contract; production-derived narrative omitted."""
    net, basis = net_realized({"realized_pnl": 6.0, "realizedPNL": 6.0,
                               "commission_unknown": True})
    assert net is None and basis == "invalid"


def test_nan_and_junk_are_not_numbers():
    for bad in (float("nan"), float("inf"), "x", None, object()):
        net, _ = net_realized({"realized_pnl": bad})
        assert net is None


def test_a_nan_fee_does_not_poison_a_net():
    net, basis = net_realized({"realized_pnl": -23.0,
                               "entry_commission": float("nan"), "exit_commission": 2.09})
    assert net is None and basis == "unknown"


def test_the_total_excludes_unknowns_rather_than_absorbing_them():
    rows = [
        {"realized_pnl": -23.0, "realized_pnl_net": -27.18},
        {"realized_pnl": 100.0},
    ]
    s = ledger_net(rows)
    assert s["total"] == -27.18, (
        "an unknown row must not contribute its GROSS to the total -- that is the original "
        "defect reintroduced one level up")
    assert s["rows_known"] == 1 and s["rows_unknown"] == 1
    assert s["known_subtotal"] == -27.18
    assert s["canonical_total"] is None and s["complete"] is False


def test_only_a_complete_ledger_has_a_canonical_total():
    s = ledger_net([
        {"realized_pnl": -23.0, "realized_pnl_net": -27.18},
        {"realized_pnl": 10.0, "entry_commission": 1.0, "exit_commission": 1.0},
    ])
    assert s["known_subtotal"] == -19.18
    assert s["canonical_total"] == -19.18 and s["complete"] is True
    assert s["rows_realized"] == 2


def test_unrealized_rows_are_ignored_entirely():
    s = ledger_net([{"symbol": "X"}, {"realized_pnl": None}])
    assert s["rows_known"] == 0 and s["rows_unknown"] == 0 and s["total"] == 0.0
    assert s["canonical_total"] == 0.0 and s["complete"] is True


def test_net_only_close_is_counted_in_the_canonical_ledger():
    summary = ledger_net([{"realized_pnl_net": 12.34}])
    assert summary["rows_realized"] == 1
    assert summary["canonical_total"] == 12.34


def test_the_basis_of_every_row_is_reported():
    """Public API contract; production-derived narrative omitted."""
    rows = [{"realized_pnl": -1.0, "realized_pnl_net": -2.0},
            {"realized_pnl": -1.0, "entry_commission": 1.0, "exit_commission": 1.0},
            {"realized_pnl": -1.0}]
    s = ledger_net(rows)
    assert s["by_basis"] == {"stored_net": 1, "gross_minus_fees": 1, "unknown": 1}
