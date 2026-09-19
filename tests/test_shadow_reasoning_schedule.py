"""Public API contract; production-derived narrative omitted."""
import asyncio
from types import SimpleNamespace

import pytest
from exitmgr.trader import Trader


def test_foreground_finishes_before_record_only_capture_and_no_task_remains():
    async def scenario():
        events = []
        async def foreground(dry_run, *, skip_exit_cycle, _shadow_contexts):
            assert dry_run is True and skip_exit_cycle is True
            _shadow_contexts.append('synthetic-reference')
            events.extend(['stage_a', 'stage_b', 'later_model_check', 'foreground_end'])
            return 'completed'
        async def capture(context):
            assert context == 'synthetic-reference'
            events.append('record_only_capture')
        trader = SimpleNamespace(_run_once=foreground, _shadow_cot_capture=capture)
        tasks_before = set(asyncio.all_tasks())
        assert await Trader.run_once(trader, True, skip_exit_cycle=True) == 'completed'
        assert events == ['stage_a', 'stage_b', 'later_model_check', 'foreground_end', 'record_only_capture']
        assert set(asyncio.all_tasks()) == tasks_before
    asyncio.run(scenario())


def test_foreground_exception_preserved_and_record_completed_before_return():
    async def scenario():
        events = []
        async def foreground(*args, _shadow_contexts, **kwargs):
            _shadow_contexts.append('synthetic-reference')
            events.append('foreground_error')
            raise ValueError('synthetic foreground failure')
        async def capture(context):
            events.append('capture')
        trader = SimpleNamespace(_run_once=foreground, _shadow_cot_capture=capture)
        with pytest.raises(ValueError, match='synthetic foreground failure'):
            await Trader.run_once(trader, False)
        assert events == ['foreground_error', 'capture']
    asyncio.run(scenario())


def test_no_stage_a_means_no_capture_and_no_reference_crosses_cycles():
    async def scenario():
        events = []
        sinks = []
        async def foreground(dry_run, *, _shadow_contexts, **kwargs):
            sinks.append(_shadow_contexts)
            events.append(('foreground', dry_run))
            if dry_run:
                _shadow_contexts.append('only-first-cycle')
        async def capture(context):
            events.append(('capture', context))
        trader = SimpleNamespace(_run_once=foreground, _shadow_cot_capture=capture)
        await Trader.run_once(trader, True)
        await Trader.run_once(trader, False)
        assert events == [('foreground', True), ('capture', 'only-first-cycle'), ('foreground', False)]
        assert sinks[0] is not sinks[1] and sinks[1] == []
    asyncio.run(scenario())


def test_capture_failure_preserves_foreground_exception():
    async def scenario():
        async def foreground(*args, _shadow_contexts, **kwargs):
            _shadow_contexts.append('synthetic-reference')
            raise ValueError('foreground remains authoritative')
        async def capture(context):
            raise OSError('audit storage unavailable')
        trader = SimpleNamespace(_run_once=foreground, _shadow_cot_capture=capture)
        with pytest.raises(ValueError, match='foreground remains authoritative'):
            await Trader.run_once(trader, False)
    asyncio.run(scenario())


def test_cancelled_foreground_never_starts_extra_inference_or_leaves_task():
    async def scenario():
        captures = []
        async def foreground(*args, _shadow_contexts, **kwargs):
            _shadow_contexts.append('synthetic-reference')
            raise asyncio.CancelledError('foreground cancelled')
        async def capture(context):
            captures.append(context)
        trader = SimpleNamespace(_run_once=foreground, _shadow_cot_capture=capture)
        before = set(asyncio.all_tasks())
        with pytest.raises(asyncio.CancelledError):
            await Trader.run_once(trader, False)
        assert captures == ['synthetic-reference']
        assert set(asyncio.all_tasks()) == before
    asyncio.run(scenario())


def test_real_capture_is_record_only_even_if_model_available(monkeypatch, tmp_path):
    from exitmgr import trader as trader_module
    model_calls = []
    records = []
    def model(*args, **kwargs):
        model_calls.append(kwargs)
        return ([], 'unused', 'unused', {'served_model': 'unused'})
    monkeypatch.setattr(trader_module, 'propose_intents', model)
    monkeypatch.setattr(trader_module, 'audit', lambda *args, **kwargs: records.append((args, kwargs)))
    trader = SimpleNamespace(model='configured-name-is-not-provenance', endpoint='synthetic',
                             audit_path=str(tmp_path / 'audit.jsonl'), journal_path=None)
    captured = {'model_was_asked': True, 'market_context': 'brief', 'raw': 'answer',
                'cot': None, 'model_identity': {'served_model': 'actual'}, 'model_identity_source': 'meta'}
    assert asyncio.run(Trader._shadow_cot_capture(trader, captured)) is None
    assert model_calls == []
    assert len(records) == 1
    record = records[0][1]
    assert records[0][0][1] == 'entry_reasoning_shadow_skipped'
    assert record['thinking'] == 'disabled'
    assert record['shadow_inference_started'] is False
    assert record['model_identity'] == {'served_model': 'actual'}
    assert record['foreground_data_ref']['cot_sha256'] is None
