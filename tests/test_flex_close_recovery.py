"""Public API contract; production-derived narrative omitted."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import socket
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest

from exitmgr.flex_close_recovery import (
    FlexCloseRefused, build_receipt, check_commit_preconditions, validate_receipt,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    monkeypatch.setattr(socket.socket, 'connect_ex', Mock(side_effect=AssertionError('network forbidden')))


@pytest.fixture
def case():
    entry = {'contract_id': 1001, 'symbol': 'FIXTURE', 'right': 'P', 'expiry': '20301231',
             'strike': 110.0, 'ts': '2026-08-18T15:00:00+00:00', 'quantity': 1,
             'spread': {'short_con_id': 1002}, 'entry_fill_debit': 220.0, 'order_ref': 'entry-fixture'}
    campaign = {'identity': ['FIXTURE', 'P', '20301231', 110.0], 'first_lot_ts': entry['ts'],
                'campaign_seq': 1, 'first_lot_line': 10}
    snapshot = {'schema': 'submitted_close.v1', 'sec_type': 'BAG', 'action': 'SELL', 'combo_qty': 1,
                'legs': [{'con_id': 1001, 'ratio': 1, 'multiplier': 100, 'expected_side': 'SLD'},
                         {'con_id': 1002, 'ratio': 1, 'multiplier': 100, 'expected_side': 'BOT'}]}
    inf = {'con_id': 1001, 'order_ref': 'close-fixture', 'perm_id': 777, 'client_id': 42, 'order_id': 6,
           'remaining_qty': 1, 'entry_debit': 220.0, 'exit_context': {'entry_debit': 220.0},
           'submitted_close': copy.deepcopy(snapshot)}
    rows = []
    for index, (cid, side, qty, price, ref, flag, stamp) in enumerate([
        (1001, 'BUY', '1', '10.00', 'entry-fixture', 'O', '20260818;110000'),
        (1002, 'SELL', '-1', '7.80', 'entry-fixture', 'O', '20260818;110000'),
        (1001, 'SELL', '-1', '11.00', 'close-fixture', 'C', '20260825;093000'),
        (1002, 'BUY', '1', '8.20', 'close-fixture', 'C', '20260825;093000'),
    ]):
        rows.append({'accountId': 'fixture-account', 'assetCategory': 'OPT', 'currency': 'USD',
                     'ibCommissionCurrency': 'USD', 'levelOfDetail': 'EXECUTION', 'conid': str(cid),
                     'buySell': side, 'quantity': qty, 'tradePrice': price, 'multiplier': '100',
                     'orderReference': ref, 'openCloseIndicator': flag, 'dateTime': stamp,
                     'ibExecID': f'fixture-exec-{index}', 'ibOrderID': f'flex-only-{index}',
                     'ibCommission': '-1.00', 'fifoPnlRealized': '28' if flag == 'C' else '0',
                     'origTradeID': '', 'notes': ''})
    return rows, dict(account_id='fixture-account', close_order_ref='close-fixture',
                      submitted_close=snapshot, entry_snapshot=entry, campaign_binding=campaign,
                      inflight_snapshot=inf, statement_timezone='America/New_York')


def encode(rows):
    tree = ET.Element('FlexQueryResponse')
    trades = ET.SubElement(tree, 'Trades')
    for row in rows:
        ET.SubElement(trades, 'Trade', row)
    raw = ET.tostring(tree)
    return raw, hashlib.sha256(raw).hexdigest()


def build(case):
    rows, kw = case
    raw, sha = encode(rows)
    return build_receipt(raw, expected_xml_sha256=sha, **kw)


def test_complete_close_preserves_actual_time_cash_fees_and_source(case):
    receipt = build(case)
    assert receipt['source'] == 'ibkr_flex'
    assert receipt['entry_fill_ts'] == '2026-08-18T15:00:00+00:00'
    assert receipt['fill_ts'] == '2026-08-25T13:30:00+00:00'
    assert receipt['net_fill_price'] == '2.80'
    assert receipt['gross_pnl'] == '60.00'
    assert receipt['entry_commission_cost'] == receipt['exit_commission_cost'] == '2.00'
    assert receipt['reconstructed_net_pnl'] == '56.00'
    assert receipt['broker_fifo_pnl_net'] == '56'
    assert len(receipt['executions']) == 2
    for row in receipt['executions']:
        assert not {'perm_id', 'client_id', 'order_id'} & row.keys()
        assert row['flex_order_id'].startswith('flex-only-')
    raw, sha = encode(case[0])
    assert validate_receipt(receipt, raw, expected_xml_sha256=sha, **case[1]) == receipt


def test_manual_close_never_invents_an_inflight_identity(case):
    case[1]['inflight_snapshot'] = None
    receipt = build(case)
    assert receipt['bindings']['inflight_sha256'] is None


@pytest.mark.parametrize('mutate', [
    lambda rows, kw: rows.pop(),
    lambda rows, kw: rows.append(copy.deepcopy(rows[-1])),
    lambda rows, kw: rows[-1].update(buySell='SELL'),
    lambda rows, kw: rows[-1].update(quantity='2'),
    lambda rows, kw: rows[-1].update(quantity='0.5'),
    lambda rows, kw: rows[-1].update(accountId='another-account'),
    lambda rows, kw: rows[-1].update(orderReference='other-close'),
    lambda rows, kw: rows[-1].update(origTradeID='corrected'),
    lambda rows, kw: rows[-1].update(notes='adjustment'),
    lambda rows, kw: rows[-1].update(ibExecID=''),
    lambda rows, kw: rows[-1].update(tradePrice='NaN'),
    lambda rows, kw: rows[-1].update(ibCommission='1'),
    lambda rows, kw: rows[-1].update(currency='EUR'),
    lambda rows, kw: rows[-1].update(dateTime='invalid'),
    lambda rows, kw: rows[-1].update(dateTime='20260817;093000'),
    lambda rows, kw: kw['submitted_close']['legs'][1].update(ratio=2),
    lambda rows, kw: kw['entry_snapshot'].update(entry_fill_debit=999),
    lambda rows, kw: kw['campaign_binding'].update(campaign_seq=0),
    lambda rows, kw: kw['campaign_binding'].update(first_lot_ts='new-campaign'),
    lambda rows, kw: kw['inflight_snapshot'].update(order_ref='different-close'),
])
def test_incomplete_conflicting_or_changed_evidence_refused(case, mutate):
    mutate(*case)
    with pytest.raises(FlexCloseRefused):
        build(case)


def test_xml_hash_and_receipt_tampering_refused(case):
    raw, sha = encode(case[0])
    with pytest.raises(FlexCloseRefused, match='digest'):
        build_receipt(raw + b' ', expected_xml_sha256=sha, **case[1])
    receipt = build(case)
    receipt['net_fill_price'] = '999'
    with pytest.raises(FlexCloseRefused):
        validate_receipt(receipt, raw, expected_xml_sha256=sha, **case[1])


def test_later_campaign_execution_is_not_hidden_by_old_close(case):
    row = copy.deepcopy(case[0][0])
    row.update(ibExecID='new-execution', orderReference='new-entry', dateTime='20260826;100000')
    case[0].append(row)
    with pytest.raises(FlexCloseRefused, match='later'):
        build(case)


def current(case):
    kw = case[1]
    now = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    return dict(entry_snapshot=kw['entry_snapshot'], campaign_binding=kw['campaign_binding'],
                inflight_snapshot=kw['inflight_snapshot'], positions={}, order_leg_con_ids=[],
                observed_at=now, now=now)


def test_commit_check_requires_current_flat_books_and_exact_state(case):
    receipt = build(case)
    check_commit_preconditions(receipt, **current(case))
    fields = current(case)
    fields['positions'] = {9999: 1}
    check_commit_preconditions(receipt, **fields)


@pytest.mark.parametrize('change', [
    lambda x: x.update(positions=None), lambda x: x.update(order_leg_con_ids=None),
    lambda x: x.update(positions={1002: -1}), lambda x: x.update(order_leg_con_ids=[1001]),
    lambda x: x.update(positions={1001: float('nan')}),
    lambda x: x.update(observed_at=x['now'] - timedelta(seconds=6)),
    lambda x: x.update(observed_at=x['now'] + timedelta(seconds=1)),
    lambda x: x.update(observed_at=x['now'].replace(tzinfo=None)),
    lambda x: x['campaign_binding'].update(campaign_seq=2),
    lambda x: x['inflight_snapshot'].update(perm_id=888),
    lambda x: x['entry_snapshot'].update(quantity=2),
])
def test_unsafe_commit_preconditions_refused(case, change):
    receipt = build(case)
    fields = copy.deepcopy(current(case))
    change(fields)
    with pytest.raises(FlexCloseRefused):
        check_commit_preconditions(receipt, **fields)


def test_manual_receipt_distinguishes_observed_topology_from_submission(case):
    case[1]['inflight_snapshot'] = None
    receipt = build(case)
    assert receipt['topology_source'] == 'journal_and_archived_executions'
    assert receipt['close_topology']['schema'] == 'close_topology.v1'
    assert 'submitted_close' not in receipt


@pytest.mark.parametrize('change', [
    lambda r: r['executions'][0].update(perm_id=777),
    lambda r: r['executions'][0].update(commission_cost='999'),
    lambda r: r['executions'][0].update(side='BOT'),
    lambda r: r['executions'][0].update(quantity=2),
    lambda r: r['entry_executions'][0].update(time='2026-08-26T15:00:00+00:00'),
    lambda r: r.update(net_fill_price='999'),
    lambda r: r.update(fill_ts='2026-09-09T15:00:00+00:00'),
    lambda r: r['bindings'].update(inflight_sha256=None),
])
def test_persisted_replay_checks_semantics_even_after_digest_recomputed(case, change):
    from exitmgr.flex_close_recovery import digest, validate_stored_receipt
    receipt = build(case)
    change(receipt)
    receipt.pop('binding_sha')
    receipt['binding_sha'] = digest(receipt)
    with pytest.raises(FlexCloseRefused):
        validate_stored_receipt(receipt)


@pytest.mark.parametrize('change', [
    lambda r: r['submitted_close'].update(legs=[None, None]),
    lambda r: r.update(statement_timezone='Invalid/Timezone'),
    lambda r: r.update(bindings=[]),
    lambda r: r['executions'][0].update(exec_id=[]),
])
def test_malformed_stored_shapes_refuse_through_public_exception(case, change):
    from exitmgr.flex_close_recovery import digest, validate_stored_receipt
    receipt = build(case)
    change(receipt)
    receipt.pop('binding_sha')
    receipt['binding_sha'] = digest(receipt)
    with pytest.raises(FlexCloseRefused):
        validate_stored_receipt(receipt)


def test_commit_refuses_string_instead_of_complete_order_collection(case):
    receipt = build(case)
    fields = current(case)
    fields['order_leg_con_ids'] = '1001'
    with pytest.raises(FlexCloseRefused):
        check_commit_preconditions(receipt, **fields)
