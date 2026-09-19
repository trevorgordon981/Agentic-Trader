"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
import copy
import inspect
import threading
from types import SimpleNamespace

import pytest
import daily_recommend as dr
from exitmgr.strategist import ModelDeadlineExceeded


def _call_sites():
    tree = ast.parse(inspect.getsource(dr.run))
    wanted = {"_mv", "broad_cands", "_ape_reviewed", "_res", "_one"}
    return {node.targets[0].id: node for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted
            and isinstance(node.value, (ast.Call, ast.Await))
            and any(isinstance(child, ast.Name)
                    and child.id in {"propose_intents", "discover_names"}
                    for child in ast.walk(node.value))}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["_mv", "broad_cands", "_ape_reviewed", "_res", "_one"])
async def test_model_wait_keeps_broker_heartbeat_responsive(name):
    calls = []
    entered, heartbeat = threading.Event(), threading.Event()
    result = object()

    def blocking_model(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        assert heartbeat.wait(0.5), "model call blocked broker event-loop callback"
        return result

    async def broker_callback():
        while not entered.is_set():
            await asyncio.sleep(0.001)
        heartbeat.set()

    node = copy.deepcopy(_call_sites()[name])
    function = ast.AsyncFunctionDef(
        name="exercise", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                           kw_defaults=[], defaults=[]),
        body=[node, ast.Return(value=ast.Name(id=name, ctx=ast.Load()))],
        decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(vars(dr))
    namespace.update(propose_intents=blocking_model, discover_names=blocking_model,
                     tr={"llm_endpoint": "http://synthetic", "llm_model": "exact-test-model"},
                     args=SimpleNamespace(ticker="SPY"), tk="SPY", brief="clean brief",
                     _brief_txt="cached brief", _ape_discovery_brief="attention brief",
                     _one_brief="fresh brief", _setup_symbols=[], _core=[], _ape_names=set(),
                     _all=[])
    exec(compile(module, inspect.getsourcefile(dr.run), "exec"), namespace)
    callback = asyncio.create_task(broker_callback())
    try:
        assert await namespace["exercise"]() is result
        await callback
    finally:
        callback.cancel()
    assert len(calls) == 1
    assert calls[0][0][:2] == ("http://synthetic", "exact-test-model")
    assert calls[0][1]["timeout"] == (600 if "cands" in name or name == "_ape_reviewed" else 1200)
    if name not in {"broad_cands", "_ape_reviewed"}:
        assert calls[0][1]["thinking"] == "enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_cancellation_keeps_guard_until_original_model_call_finishes(tmp_path, fails):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, consumed = [], []
    flag = tmp_path / "slate.active"

    def blocking_model(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(1), "test failed to release original request"
        finished.set()
        if fails:
            raise TimeoutError("original request deadline")
        return "discard this canceled result"

    async def slate():
        with dr.slate_active_guard(path=str(flag)):
            result = await dr._await_slate_model(blocking_model, timeout=600)
            consumed.append(result)

    task = asyncio.create_task(slate())
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert flag.exists() and not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not flag.exists()
        assert calls == [{"timeout": 600}] and consumed == []
    finally:
        release.set()
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_slate_model_wrapper_preserves_context_and_original_exception():
    import contextvars
    marker = contextvars.ContextVar("test_workload", default="wrong")
    marker.set("slate")
    problem = ModelDeadlineExceeded("existing absolute model deadline")
    def model():
        assert marker.get() == "slate"
        raise problem
    with pytest.raises(ModelDeadlineExceeded) as caught:
        await dr._await_slate_model(model)
    assert caught.value is problem
