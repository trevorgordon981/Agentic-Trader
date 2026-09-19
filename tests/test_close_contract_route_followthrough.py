"""Public API contract; production-derived narrative omitted."""
from types import SimpleNamespace
import pytest
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Contract


def make_conn(*, currency='USD', exchange='', sec_type='OPT'):
    contract = Contract(conId=5001001, symbol='SYMD', right='C', secType=sec_type,
                        currency=currency, exchange=exchange, multiplier='100',
                        lastTradeDateOrContractMonth='20261120', strike=145)
    conn = IBConnection('127.0.0.1', 4002, 118)
    conn.ib = SimpleNamespace(positions=lambda: [SimpleNamespace(contract=contract)],
                              portfolio=lambda: [])
    return conn, contract


def test_known_us_option_gets_explicit_route_without_mutating_broker_cache():
    conn, source = make_conn()
    close = conn.create_contract(5001001, symbol='SYMD', right='C')
    assert close.exchange == 'SMART'
    assert close.currency == 'USD'
    assert close.lastTradeDateOrContractMonth == '20261120'
    assert close.strike == 145 and close.multiplier == '100'
    assert source.exchange == '' and close is not source


@pytest.mark.parametrize('currency,exchange,sec_type', [
    ('EUR', 'EUREX', 'OPT'), ('CAD', 'MONTREAL', 'OPT'), ('USD', 'CME', 'FOP'),
    ('USD', 'CBOE', 'OPT'),
])
def test_exact_native_route_and_currency_are_preserved(currency, exchange, sec_type):
    conn, source = make_conn(currency=currency, exchange=exchange, sec_type=sec_type)
    close = conn.create_contract(5001001, 'SYMD', 'C')
    assert (close.currency, close.exchange, close.secType) == (currency, exchange, sec_type)


@pytest.mark.parametrize('currency,sec_type', [('EUR', 'OPT'), ('USD', 'FOP'), ('', 'OPT')])
def test_unknown_foreign_route_or_currency_is_refused(currency, sec_type):
    conn, _ = make_conn(currency=currency, sec_type=sec_type)
    with pytest.raises(ValueError, match='cannot route close'):
        conn.create_contract(5001001, 'SYMD', 'C')


@pytest.mark.parametrize('cid,symbol,right', [(123, 'SYMD', 'C'),
                                            (5001001, 'SYMD', 'P'),
                                            (5001001, 'OTHER', 'C')])
def test_unknown_or_contradictory_contract_is_refused(cid, symbol, right):
    conn, _ = make_conn()
    with pytest.raises(ValueError, match='cannot route close'):
        conn.create_contract(cid, symbol, right)


def test_missing_caller_right_uses_exact_broker_identity():
    conn, _ = make_conn()
    assert conn.create_contract(5001001, 'SYMD', '').right == 'C'


def test_put_close_retains_broker_right_without_default_call_relabeling():
    conn, source = make_conn()
    source.right = 'P'
    close = conn.create_contract(5001001, 'SYMD', 'P')
    assert close.right == 'P' and close.exchange == 'SMART'
    with pytest.raises(ValueError, match='identity mismatch'):
        conn.create_contract(5001001, 'SYMD')
