"""Public API contract; production-derived narrative omitted."""
from typing import Any, Callable, List, Union

import exitmgr.trader as trader

__all__ = ["stub_stage_a"]

Ret = Union[List[Any], tuple, Callable[..., Any]]


def stub_stage_a(monkeypatch, ret: Ret, *, materialize=None):
    """Public API contract; production-derived narrative omitted."""

    def _value(*a, **k):
        return ret(*a, **k) if callable(ret) else ret

    def _propose_intents(*a, **k):

        return _value(*a, **k)

    async def _materialize_stage_b(self, intents, pot):
        if materialize is not None:
            result = materialize(self, intents, pot)
            if hasattr(result, "__await__"):
                return await result
            return result
        return list(intents or [])

    monkeypatch.setattr(trader, "propose_intents", _propose_intents)
    monkeypatch.setattr(trader.Trader, "_materialize_stage_b", _materialize_stage_b)



    def _retired(*a, **k):
        raise AssertionError(
            "trader.propose is retired -- run_once calls propose_intents/_materialize_stage_b. "
            "Use tests._stage_stub.stub_stage_a instead of patching propose.")

    monkeypatch.setattr(trader, "propose", _retired, raising=False)
