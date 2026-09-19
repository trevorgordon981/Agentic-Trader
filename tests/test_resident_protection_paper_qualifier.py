"""Public API contract; production-derived narrative omitted."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

import qualify_resident_protection as q
from exitmgr.resident_protection import plan_shadow


def _plan(spread=False, quantity=1):
    journal = {"spread": {"short_con_id": 222}} if spread else {}
    return plan_shadow(con_id=111, symbol="SPY", quantity=quantity,
                       entry_debit=200.0 * quantity,
                       stop_pct=30.0, journal=journal,
                       campaign={"campaign_seq": 1, "identity": ["SPY", "C"]},
                       code_version="a" * 40, policy_version="b" * 64)


@pytest.mark.parametrize("accounts", [[], ["U123"], ["DU1", "DU2"], ["DU1", "U2"]])
def test_nonpaper_or_ambiguous_accounts_are_refused(accounts):
    with pytest.raises(q.PaperQualificationError, match="refusing broker mutation"):
        q._paper_account(accounts)


def test_one_paper_account_is_accepted_without_disclosing_it():
    assert q._paper_account(["DU12345"]) == "DU12345"


@pytest.mark.parametrize("port", [4001, 7496, 1, 8080])
@pytest.mark.asyncio
async def test_live_and_arbitrary_ports_refuse_before_constructing_ib(tmp_path, port):
    called = []
    with pytest.raises(q.PaperQualificationError, match="refusing non-paper port"):
        await q.qualify(host="127.0.0.1", port=port, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "r.json",
                        ib_factory=lambda: called.append(True))
    assert called == []


def test_only_bound_shadow_plans_are_eligible():
    plan = _plan()
    assert q._validated_plan(plan)["binding_sha"] == plan["binding_sha"]
    for key, value in (("approved", True), ("authority", "LIVE"),
                       ("mode", "fixed_stop"), ("order_type", "MKT")):
        changed = dict(plan, **{key: value})
        with pytest.raises(q.PaperQualificationError):
            q._validated_plan(changed)


@pytest.mark.parametrize("key,value", [
    ("aux_price", 999.0),
    ("quantity", 2),
    ("policy_version", "c" * 64),
])
def test_stale_planner_binding_is_refused_after_bound_fields_change(key, value):
    changed = dict(_plan(), **{key: value})
    with pytest.raises(q.PaperQualificationError, match="binding_sha does not match"):
        q._validated_plan(changed)


def test_inventory_gate_proves_close_side_for_single_and_bag():
    pos = lambda cid, qty: SimpleNamespace(
        contract=SimpleNamespace(conId=cid), position=qty)
    q._require_closing_inventory([pos(111, 1)], _plan())
    q._require_closing_inventory([pos(111, 1), pos(222, -1)], _plan(True))
    with pytest.raises(q.PaperQualificationError, match="long closing inventory"):
        q._require_closing_inventory([], _plan())
    with pytest.raises(q.PaperQualificationError, match="short closing inventory"):
        q._require_closing_inventory([pos(111, 1), pos(222, 1)], _plan(True))


def test_contracts_and_orders_preserve_exact_topology_without_live_authority():
    single, one_order = q._contract_and_order(_plan())
    assert single.secType == "OPT" and single.conId == 111
    assert one_order.action == "SELL" and one_order.orderType == "STP"
    assert one_order.tif == "GTC" and one_order.transmit is True
    assert one_order.triggerMethod == 1
    bag, bag_order = q._contract_and_order(_plan(True))
    assert bag.secType == "BAG"
    assert [(leg.conId, leg.action) for leg in bag.comboLegs] == [(111, "BUY"), (222, "SELL")]
    assert bag_order.orderRef.startswith("paper-qual:resident-v1:")
    assert bag_order.triggerMethod == 4


@pytest.mark.parametrize("field,value", [
    ("trigger_method", 0), ("trigger_method", 7), ("exchange", "CBOE"),
    ("currency", "EUR"), ("sec_type", "STK"),
])
@pytest.mark.asyncio
async def test_unsupported_shape_refused_even_if_rebound_before_connector(tmp_path, field, value):
    plan = dict(_plan(True), **{field: value})
    plan["binding_sha"] = q._planner_binding_sha(plan)
    called = []
    with pytest.raises(q.PaperQualificationError):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=plan, receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: called.append(True))
    assert not called


def test_capability_cannot_claim_production_or_outage_proof_even_when_rebound():
    for field in ("production_qualified", "gateway_outage_residency_proven",
                  "exact_contract_route_qualified", "oca_overfill_protection_assumed"):
        plan = copy.deepcopy(_plan(True))
        plan["capability"][field] = True
        plan["binding_sha"] = q._planner_binding_sha(plan)
        with pytest.raises(q.PaperQualificationError, match="falsely qualified"):
            q._validated_plan(plan)


def test_rebound_reverse_topology_is_not_a_debit_close():
    plan = copy.deepcopy(_plan(True))
    plan["topology"][0]["combo_action"] = "SELL"
    plan["binding_sha"] = q._planner_binding_sha(plan)
    with pytest.raises(q.PaperQualificationError, match="closing topology"):
        q._validated_plan(plan)


def test_receipt_write_is_atomic_json(tmp_path):
    out = tmp_path / "nested" / "receipt.json"
    q._write_receipt(out, {"status": "COMPLETE", "value": 1})
    assert out.read_text() == '{"status":"COMPLETE","value":1}\n'
    with pytest.raises(q.PaperQualificationError, match="refusing to overwrite"):
        q._write_receipt(out, {"status": "different"})
    assert out.read_text() == '{"status":"COMPLETE","value":1}\n'


class _FakePaperIB:
    def __init__(self, account="DU123", quantity=1):
        self.account = account
        self.quantity = quantity
        self.connected = False
        self.working = []
        self.place_calls = 0
        self.connect_calls = 0
        self.cancel_calls = 0

    async def connectAsync(self, *args, **kwargs):
        self.connected = True
        self.connect_calls += 1
        return self

    def managedAccounts(self):
        return [self.account]

    def positions(self):
        return [SimpleNamespace(
            contract=SimpleNamespace(conId=111), position=self.quantity)]

    def placeOrder(self, contract, order):
        self.place_calls += 1
        order.orderId = 17
        order.permId = 1700
        trade = SimpleNamespace(
            contract=contract, order=order,
            orderStatus=SimpleNamespace(
                status="Submitted", filled=0.0,
                remaining=float(order.totalQuantity)))
        self.working.append(trade)
        return trade

    async def reqOpenOrdersAsync(self):
        return list(self.working)

    def cancelOrder(self, order):
        self.cancel_calls += 1
        for trade in list(self.working):
            if trade.order is order:
                trade.orderStatus.status = "Cancelled"
                self.working.remove(trade)
                return trade
        raise AssertionError("unknown order")

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False


@pytest.mark.asyncio
async def test_complete_paper_sequence_places_reconnects_cancels_and_proves_absence(tmp_path):
    fake = _FakePaperIB()
    out = tmp_path / "paper.json"
    receipt = await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                              plan=_plan(), receipt_path=out,
                              ib_factory=lambda: fake, timeout_s=0.5)
    assert receipt["status"] == "COMPLETE"
    assert receipt["authority"] == "PAPER_EVIDENCE_ONLY_NO_LIVE_AUTHORITY"
    assert receipt["production_activation_approved"] is False
    assert receipt["qualification_scope"] == "PAPER_API_SOCKET_RECONNECT_AND_CLEANUP_ONLY"
    assert receipt["api_socket_reconnect_proven"] is True
    assert receipt["reconnect_order_fields_match_plan"] is True
    for field in ("production_qualified", "gateway_shutdown_tested", "trader_host_outage_tested",
                  "gateway_outage_residency_proven", "stop_election_proven",
                  "native_exchange_residency_proven", "single_owner_handoff_proven"):
        assert receipt[field] is False
    assert receipt["zero_fill_proven"] is True
    assert receipt["full_remaining_before_cancel_proven"] is True
    assert receipt["placed"]["filled"] == 0.0
    assert receipt["placed"]["remaining"] == 1.0
    assert receipt["discovered"]["order_id"] == receipt["placed"]["order_id"]
    assert receipt["discovered"]["perm_id"] == receipt["placed"]["perm_id"]
    assert receipt["phases"] == [
        "PAPER_ACCOUNT_VERIFIED", "CLOSING_INVENTORY_VERIFIED", "PLACED_WORKING",
        "RECONNECT_DISCOVERED", "CANCEL_CONFIRMED", "SECOND_RECONNECT_ABSENT",
    ]
    assert fake.place_calls == 1 and fake.connect_calls == 3 and fake.working == []
    assert '"status":"COMPLETE"' in out.read_text()


@pytest.mark.asyncio
async def test_live_account_gate_precedes_inventory_and_place(tmp_path):
    fake = _FakePaperIB(account="U123")
    with pytest.raises(q.PaperQualificationError, match="refusing broker mutation"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.place_calls == 0
    assert not (tmp_path / "never.json").exists()


class _FirstReconnectFails(_FakePaperIB):
    async def connectAsync(self, *args, **kwargs):
        self.connect_calls += 1
        if self.connect_calls == 2:
            self.connected = False
            raise OSError("simulated first reconnect failure")
        self.connected = True
        return self


class _BrokerEvidenceArrivesAfterLocalPendingSubmit(_FakePaperIB):
    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        order.permId = 0
        trade.orderStatus.permId = 0
        trade.orderStatus.status = "PendingSubmit"
        trade.orderStatus.remaining = 0.0

        def _broker_update():
            order.permId = 1700
            trade.orderStatus.permId = 1700
            trade.orderStatus.status = "Submitted"
            trade.orderStatus.remaining = float(order.totalQuantity)

        asyncio.get_running_loop().call_soon(_broker_update)
        return trade


@pytest.mark.asyncio
async def test_local_pending_submit_waits_for_broker_identity_and_quantities(tmp_path):
    fake = _BrokerEvidenceArrivesAfterLocalPendingSubmit()
    receipt = await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                              plan=_plan(), receipt_path=tmp_path / "receipt.json",
                              ib_factory=lambda: fake, timeout_s=0.5)
    assert receipt["placed"]["status"] == "Submitted"
    assert receipt["placed"]["perm_id"] == 1700
    assert fake.working == []


@pytest.mark.asyncio
async def test_post_transmit_reconnect_failure_recovers_and_cancels(tmp_path):
    fake = _FirstReconnectFails()
    with pytest.raises(OSError, match="simulated first reconnect failure"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.connect_calls == 4
    assert fake.cancel_calls == 1
    assert fake.working == []
    assert not (tmp_path / "never.json").exists()


class _ReconnectNeverRecovers(_FakePaperIB):
    async def connectAsync(self, *args, **kwargs):
        self.connect_calls += 1
        if self.connect_calls > 1:
            self.connected = False
            raise OSError("paper gateway unavailable")
        self.connected = True
        return self


@pytest.mark.asyncio
async def test_cleanup_reconnect_attempts_are_bounded_and_failure_is_explicit(tmp_path):
    fake = _ReconnectNeverRecovers()
    with pytest.raises(q.PaperQualificationError, match="unproven paper cleanup"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.connect_calls == 2 + q.CLEANUP_ATTEMPTS
    assert fake.working
    assert not (tmp_path / "never.json").exists()


class _ReconnectDriftsToLiveAccount(_FakePaperIB):
    async def connectAsync(self, *args, **kwargs):
        self.connect_calls += 1
        self.connected = True
        if self.connect_calls > 1:
            self.account = "U-LIVE"
        return self


@pytest.mark.asyncio
async def test_cleanup_never_cancels_after_reconnect_drifts_to_live_account(tmp_path):
    fake = _ReconnectDriftsToLiveAccount()
    with pytest.raises(q.PaperQualificationError, match="unproven paper cleanup"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.cancel_calls == 0
    assert not (tmp_path / "never.json").exists()


class _FirstCancelFails(_FakePaperIB):
    def cancelOrder(self, order):
        self.cancel_calls += 1
        if self.cancel_calls == 1:
            raise OSError("simulated cancel failure")

        self.cancel_calls -= 1
        return super().cancelOrder(order)


@pytest.mark.asyncio
async def test_post_transmit_cancel_failure_is_retried_and_absence_proven(tmp_path):
    fake = _FirstCancelFails()
    with pytest.raises(OSError, match="simulated cancel failure"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.cancel_calls == 2
    assert fake.working == []
    assert not (tmp_path / "never.json").exists()


class _PartialFillOnCancel(_FakePaperIB):
    def cancelOrder(self, order):
        self.cancel_calls += 1
        for trade in list(self.working):
            if trade.order is order:
                trade.orderStatus.filled = 1.0
                trade.orderStatus.remaining = float(order.totalQuantity) - 1.0
                trade.orderStatus.status = "Cancelled"
                self.working.remove(trade)
                return trade
        raise AssertionError("unknown order")


@pytest.mark.asyncio
async def test_partial_fill_then_cancel_can_never_produce_complete_receipt(tmp_path):
    fake = _PartialFillOnCancel(quantity=2)
    with pytest.raises(q.PaperQualificationError, match="cannot prove zero fill"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(quantity=2), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.working == []
    assert not (tmp_path / "never.json").exists()


class _MissingQuantityEvidence(_FakePaperIB):
    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        trade.orderStatus.remaining = None
        return trade


@pytest.mark.asyncio
async def test_missing_remaining_evidence_fails_closed_and_cleans_up(tmp_path):
    fake = _MissingQuantityEvidence()
    with pytest.raises(q.PaperQualificationError, match="full remaining quantity"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.working == []
    assert not (tmp_path / "never.json").exists()


class _IdentityDriftsOnReconnect(_FakePaperIB):
    def __init__(self, identity_field):
        super().__init__()
        self.identity_field = identity_field

    async def reqOpenOrdersAsync(self):
        if self.connect_calls == 2 and self.working:
            current = getattr(self.working[0].order, self.identity_field)
            setattr(self.working[0].order, self.identity_field, current + 1)
        return list(self.working)


@pytest.mark.parametrize("identity_field", ["orderId", "permId"])
@pytest.mark.asyncio
async def test_reconnect_identity_drift_fails_closed_and_cleans_namespace(
        tmp_path, identity_field):
    fake = _IdentityDriftsOnReconnect(identity_field)
    with pytest.raises(q.PaperQualificationError, match="broker identity changed"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.working == []
    assert fake.cancel_calls == 1
    assert not (tmp_path / "never.json").exists()


class _MissingBrokerIdentity(_FakePaperIB):
    def __init__(self, identity_field):
        super().__init__()
        self.identity_field = identity_field

    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        setattr(trade.order, self.identity_field, 0)
        return trade


@pytest.mark.parametrize("identity_field", ["orderId", "permId"])
@pytest.mark.asyncio
async def test_missing_broker_identity_fails_closed_and_cleans_up(tmp_path, identity_field):
    fake = _MissingBrokerIdentity(identity_field)
    with pytest.raises(q.PaperQualificationError, match="identity is unavailable"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert fake.working == []
    assert not (tmp_path / "never.json").exists()


class _PlanFieldDriftsOnReconnect(_FakePaperIB):
    async def reqOpenOrdersAsync(self):
        if self.connect_calls == 2 and self.working:
            self.working[0].order.triggerMethod = 0
        return list(self.working)


@pytest.mark.asyncio
async def test_reconnect_trigger_drift_cannot_produce_success_receipt(tmp_path):
    fake = _PlanFieldDriftsOnReconnect()
    with pytest.raises(q.PaperQualificationError, match="field mismatch: triggerMethod"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.5)
    assert not fake.working
    assert not (tmp_path / "never.json").exists()


def test_bag_reconnect_verifies_both_legs_and_route():
    plan = _plan(True)
    contract, order = q._contract_and_order(plan)
    trade = SimpleNamespace(contract=contract, order=order)
    q._require_plan_fields(trade, plan)
    contract.comboLegs[1].conId += 1
    with pytest.raises(q.PaperQualificationError, match="comboLegs"):
        q._require_plan_fields(trade, plan)


class _PendingWithKnownIdentity(_FakePaperIB):
    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        trade.orderStatus.status = "PendingSubmit"
        return trade


@pytest.mark.asyncio
async def test_pending_submit_is_not_broker_acceptance_even_with_nonzero_identity(tmp_path):
    fake = _PendingWithKnownIdentity()
    with pytest.raises(q.PaperQualificationError, match="placement evidence timeout"):
        await q.qualify(host="127.0.0.1", port=4002, client_id=119,
                        plan=_plan(), receipt_path=tmp_path / "never.json",
                        ib_factory=lambda: fake, timeout_s=0.2)
    assert not fake.working
    assert not (tmp_path / "never.json").exists()
