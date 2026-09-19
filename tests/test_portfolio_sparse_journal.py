"""Public API contract; production-derived narrative omitted."""
import ast
import copy
from datetime import datetime, date, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'portfolio.py'

@pytest.fixture
def review(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    monkeypatch.setattr(socket.socket, 'connect_ex', Mock(side_effect=AssertionError('network forbidden')))
    monkeypatch.setitem(sys.modules, 'book_return', SimpleNamespace(book_return=lambda n: None))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    journal = tmp_path / 'journal.jsonl'
    tree = ast.parse(SOURCE.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in ('_load_journal', '_days_held', 'review_positions')]
    scope = {'json': json, 'os': os, 'datetime': datetime, 'date': date, 'timezone': timezone,
             '_LEGACY_JOURNAL_TZ': ZoneInfo('America/Los_Angeles'), '__file__': str(SOURCE),
             'JOURNAL': str(journal), 'REVIEW_PROMPT': 'fixture advisory',
             'get_pot_snapshot': AsyncMock(return_value=SimpleNamespace(available_funds=100, net_liq=1000)),
             '_price': AsyncMock(return_value=(110, 10)),
             '_last_json': json.loads, '_llm': Mock(return_value='{"reviews": [], "rotation": {"sell": null}}')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), 'exec'), scope)
    return scope, journal


def entry(cid=1):
    return {'contract_id': cid, 'symbol': 'FIXTURE', 'right': 'C', 'strike': 10.0,
            'expiry': '20301231', 'quantity': 1, 'debit': 100.0,
            'ts': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}


def broker(*ids):
    return SimpleNamespace(reqPositionsAsync=AsyncMock(return_value=[
        SimpleNamespace(contract=SimpleNamespace(conId=abs(i)), position=1 if i>0 else -1) for i in ids]))

@pytest.mark.asyncio
async def test_sparse_close_marker_for_live_short_does_not_abort_other_rows(review, capsys):
    scope, journal = review
    marker={'contract_id': 2, 'symbol': 'PAIRED', 'event': 'position_closed', 'ts': entry()['ts']}

    rows=[entry(2), marker, entry(1)]
    journal.write_text('\n'.join(map(json.dumps, rows)))
    before=journal.read_bytes()
    result=await scope['review_positions'](broker(-2, 1))
    assert [r['con_id'] for r in result['book']]==[1]
    assert result['book'][0]['cost_usd']==100
    assert result['unreviewable']==[{'con_id': 2, 'reason': 'unreviewable journal row: latest journal row is a close marker, not an entry'}]
    assert 'con_id=2' in capsys.readouterr().out
    scope['_price'].assert_awaited_once()
    assert scope['_price'].call_args.args[1]['contract_id']==1
    payload=json.loads(scope['_llm'].call_args.args[1])
    assert payload['unreviewable']==result['unreviewable']
    assert len(payload['book'])==1
    assert journal.read_bytes()==before

@pytest.mark.asyncio
@pytest.mark.parametrize('change', [lambda r:r.pop('expiry'), lambda r:r.update(expiry='not-date'),
    lambda r:r.pop('strike'), lambda r:r.pop('debit'), lambda r:r.pop('quantity'),
    lambda r:r.update(debit=float('nan')), lambda r:r.update(quantity=0)])
async def test_missing_or_invalid_metadata_is_explicit_skip_without_price_or_model(review, change):
    scope,journal=review
    row=entry();change(row);journal.write_text(json.dumps(row)+'\n')
    result=await scope['review_positions'](broker(1))
    assert result['book']==[]
    assert result['reviews']==[]
    assert result['rotation']=={'sell':None}
    assert len(result['unreviewable'])==1
    assert result['unreviewable'][0]['con_id']==1
    assert result['unreviewable'][0]['reason'].startswith('unreviewable journal row:')
    scope['_price'].assert_not_awaited()
    scope['_llm'].assert_not_called()

@pytest.mark.asyncio
async def test_complete_spread_keeps_existing_structure_and_basis(review):
    scope,journal=review
    row=entry();row['spread']={'short_con_id': 2, 'short_strike': 12.0}
    journal.write_text(json.dumps(row)+'\n')
    result=await scope['review_positions'](broker(1,-2))
    assert result['book'][0]['structure']=='FIXTURE 20301231 10/12C'
    assert result['book'][0]['cost_usd']==100
    assert result['book'][0]['value_usd']==110
    assert result['book'][0]['pnl_pct']==10
    assert result['unreviewable']==[]
