"""Public API contract; production-derived narrative omitted."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_production_build_brief_call_supplies_account_inputs():
    files = [
        ROOT / "exitmgr" / "trader.py",
        ROOT / "daily_recommend.py",
        ROOT / "morning_review.py",
        ROOT / "intraday.py",
    ]
    calls = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "build_brief":
                continue
            keys = {kw.arg for kw in node.keywords if kw.arg is not None}
            calls.append((path.name, node.lineno, keys))
    assert len(calls) == 5
    missing = [(name, line, sorted({"net_liq", "available_funds"} - keys))
               for name, line, keys in calls
               if not {"net_liq", "available_funds"} <= keys]
    assert missing == []


def test_trader_fallback_and_add_name_refresh_account_snapshot():
    trader = (ROOT / "exitmgr" / "trader.py").read_text()
    daily = (ROOT / "daily_recommend.py").read_text()
    assert "research.with_account_sizing_snapshot(" in trader
    assert "research.with_account_sizing_snapshot(" in daily
    assert "_one_pot = await get_pot_snapshot(ib)" in daily
