"""Tests for the strategist's hard JSON validation."""
from exitmgr.strategist import parse_ideas, TradeIdea


def test_valid_single_index_trade():
    raw = '{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish","structure":"long call","target_dte":7,"target_delta":0.35,"est_debit_usd":90,"conviction":4,"thesis":"trend up"}]}'
    ideas = parse_ideas(raw)
    assert len(ideas) == 1
    assert ideas[0].underlying == "SPY" and ideas[0].is_index and ideas[0].conviction == 4


def test_json_embedded_in_prose_is_extracted():
    raw = 'Sure, here is my idea:\n{"trades":[{"underlying":"qqq","direction":"bearish","structure":"long put","target_dte":5,"target_delta":0.3,"est_debit_usd":80,"conviction":3,"thesis":"weak"}]} hope that helps'
    ideas = parse_ideas(raw)
    assert len(ideas) == 1 and ideas[0].underlying == "QQQ" and ideas[0].direction == "bearish"


def test_empty_list_when_nothing_compelling():
    assert parse_ideas('{"trades":[]}') == []


def test_malformed_json_drops_everything():
    assert parse_ideas("the market looks choppy, no JSON here") == []
    assert parse_ideas('{"trades":[{bad json]}') == []


def test_out_of_bounds_normalized_or_dropped():
    # conviction>10 and delta>1 are CLAMPED (kept); bad direction and negative debit are dropped
    raw = ('{"trades":['
           '{"underlying":"SPY","direction":"bullish","structure":"x","target_dte":7,"target_delta":1.7,"est_debit_usd":50,"conviction":9,"thesis":"clamp"},'
           '{"underlying":"SPY","direction":"sideways","structure":"iron condor","target_dte":7,"target_delta":0.3,"est_debit_usd":50,"conviction":3,"thesis":"drop-dir"},'
           '{"underlying":"SPY","direction":"bullish","structure":"x","target_dte":7,"target_delta":0.3,"est_debit_usd":-5,"conviction":3,"thesis":"drop-debit"}'
           ']}')
    ideas = parse_ideas(raw)
    assert len(ideas) == 1 and ideas[0].conviction == 9 and ideas[0].target_delta == 1.0


def test_missing_required_field_dropped_but_keeps_valid_sibling():
    raw = ('{"trades":['
           '{"underlying":"SPY","direction":"bullish","target_delta":0.3,"est_debit_usd":50,"conviction":3,"thesis":"no dte"},'
           '{"underlying":"NVDA","is_index":false,"direction":"bullish","structure":"long call","target_dte":10,"target_delta":0.4,"est_debit_usd":70,"conviction":4,"thesis":"ok"}'
           ']}')
    ideas = parse_ideas(raw)
    assert len(ideas) == 1 and ideas[0].underlying == "NVDA" and not ideas[0].is_index


def test_is_index_inferred_when_missing():
    raw = '{"trades":[{"underlying":"IWM","direction":"bullish","structure":"long call","target_dte":7,"target_delta":0.3,"est_debit_usd":40,"conviction":3,"thesis":"x"}]}'
    assert parse_ideas(raw)[0].is_index is True  # inferred from universe


def test_sell_levels_parsed_and_clamped():
    raw = ("{\"trades\":[{\"underlying\":\"IWM\",\"is_index\":true,\"direction\":\"bullish\","
           "\"structure\":\"long call\",\"target_dte\":20,\"target_delta\":0.4,\"est_debit_usd\":600,"
           "\"conviction\":7,\"profit_target_pct\":75,\"stop_pct\":40,\"thesis\":\"x\"}]}")
    i = parse_ideas(raw)[0]
    assert i.profit_target_pct == 75.0 and i.stop_pct == 40.0


def test_sell_levels_default_when_missing_or_bad():
    raw = ("{\"trades\":[{\"underlying\":\"SPY\",\"is_index\":true,\"direction\":\"bullish\","
           "\"structure\":\"long call\",\"target_dte\":7,\"target_delta\":0.35,\"est_debit_usd\":90,"
           "\"conviction\":6,\"stop_pct\":999,\"thesis\":\"x\"}]}")
    i = parse_ideas(raw)[0]
    assert i.profit_target_pct == 0.0       # missing -> 0 (manager uses global default)
    assert i.stop_pct == 90.0               # 999 clamped to 90
