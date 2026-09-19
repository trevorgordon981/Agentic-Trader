"""Public API contract; production-derived narrative omitted."""

import math

import pytest

from exitmgr.resident_protection import (
    ResidentProtectionPlanError, plan_shadow, shadow_handoff_decision,
)


BASE = {
    "con_id": 123456, "symbol": "AAPL", "quantity": 2,
    "entry_debit": 500.0, "stop_pct": 30.0,
    "journal": {}, "campaign": {"campaign_seq": 1, "identity": ["AAPL", "C"]},
    "code_version": "a" * 40, "policy_version": "b" * 64,
}


def test_shadow_plan_is_explicitly_non_authoritative_and_complete():
    plan = plan_shadow(**BASE)
    assert plan["authority"] == "SHADOW_ONLY_NO_BROKER_ACTION"
    assert plan["approved"] is False and plan["mode"] == "shadow"
    assert plan["sec_type"] == "OPT" and plan["action"] == "SELL"
    assert plan["order_type"] == "STP" and plan["tif"] == "GTC"
    assert plan["outside_rth"] is False
    assert plan["aux_price"] == 1.75
    assert plan["topology"] == [{
        "con_id": 123456, "ratio": 1, "combo_action": "BUY", "close_side": "SLD",
    }]
    assert plan["order_ref"].startswith("exitmgr-protect-v1:123456:")
    assert len(plan["binding_sha"]) == 64


def test_spread_plan_binds_both_legs_and_disclaims_atomicity():
    plan = plan_shadow(**dict(BASE, journal={"spread": {"short_con_id": 654321}}))
    assert plan["sec_type"] == "BAG"
    assert plan["topology"] == [
        {"con_id": 123456, "ratio": 1, "combo_action": "BUY", "close_side": "SLD"},
        {"con_id": 654321, "ratio": 1, "combo_action": "SELL", "close_side": "BOT"},
    ]
    assert "may execute separately" in plan["routing_caveat"]
    assert plan["trigger_method"] == 4
    assert plan["capability"]["status"] == "UNQUALIFIED_EXACT_CONTRACT_ROUTE"
    assert plan["capability"]["gateway_outage_residency_proven"] is False
    assert plan["capability"]["production_qualified"] is False
    assert plan["capability"]["oca_overfill_protection_assumed"] is False


def _handoff(**changes):
    args = dict(resident_status="Cancelled", identity_matches=True, orders_known=True,
                executions_reconciled=True, inventory_fresh=True,
                remaining_long=2, remaining_short=-2)
    args.update(changes)
    result = shadow_handoff_decision(**args)
    assert result["broker_action_authorized"] is False
    assert result["authority"] == "SHADOW_ONLY_NO_BROKER_ACTION"
    return result


@pytest.mark.parametrize("status", ["PendingCancel", "PendingSubmit", "ApiPending"])
def test_handoff_pending_never_releases_owner(status):
    result = _handoff(resident_status=status)
    assert result["next_step"] == "WAIT_FOR_BROKER_TERMINAL_STATE"
    assert result["replacement_quantity"] is None


@pytest.mark.parametrize("field", ["identity_matches", "orders_known",
                                   "executions_reconciled", "inventory_fresh"])
def test_handoff_requires_all_fresh_reconciled_evidence(field):
    assert _handoff(**{field: False})["replacement_quantity"] is None


def test_handoff_cancel_fill_race_uses_only_residual_inventory():
    result = _handoff(remaining_long=1, remaining_short=-1)
    assert result["replacement_quantity"] == 1
    assert result["protection_gap"] is True
    assert _handoff(remaining_long=0, remaining_short=0)["next_step"] == "FLAT_NO_REPLACEMENT"


@pytest.mark.parametrize("long_qty,short_qty", [
    (2, -1), (1, 0), (0, -1), (-1, 1), (math.nan, -1),
    (math.inf, -1), (True, -1), (1.5, -1.5), (None, None),
])
def test_handoff_never_guesses_quantity_for_missing_or_unbalanced_legs(long_qty, short_qty):
    assert _handoff(remaining_long=long_qty, remaining_short=short_qty)["replacement_quantity"] is None


def test_handoff_working_resident_cannot_coexist_with_new_full_close():
    result = _handoff(resident_status="Submitted")
    assert result["next_step"] == "REQUEST_OWNER_HANDOFF_NO_SECOND_ORDER"
    assert result["replacement_quantity"] is None


@pytest.mark.parametrize("field,value", [
    ("entry_debit", 600.0), ("stop_pct", 20.0), ("quantity", 1),
    ("symbol", "MSFT"), ("code_version", "c" * 40),
    ("policy_version", "d" * 64),
    ("campaign", {"campaign_seq": 2, "identity": ["AAPL", "C"]}),
])
def test_every_authority_input_changes_the_immutable_binding(field, value):
    original = plan_shadow(**BASE)
    changed = plan_shadow(**dict(BASE, **{field: value}))
    assert changed["binding_sha"] != original["binding_sha"]
    assert changed["order_ref"] != original["order_ref"]


@pytest.mark.parametrize("field,value", [
    ("entry_debit", math.nan), ("entry_debit", math.inf),
    ("stop_pct", math.nan), ("stop_pct", math.inf),
    ("stop_pct", 30.1), ("stop_pct", 0), ("quantity", 0),
])
def test_unusable_or_wider_than_doctrine_inputs_fail_closed(field, value):
    with pytest.raises(ResidentProtectionPlanError):
        plan_shadow(**dict(BASE, **{field: value}))
