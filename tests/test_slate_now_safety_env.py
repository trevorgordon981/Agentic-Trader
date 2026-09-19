from pathlib import Path


SCRIPT = (Path(__file__).parents[1] / "slate-now.sh").read_text()


def test_manual_slate_matches_scheduled_model_safety_environment():
    required = (
        "export STRATEGIST_RAG_ENABLED=1",
        "export TRADER_LLM_PRIORITY=0",
        "export TRADER_REQUIRE_PRIORITY_TOKEN=1",
        "export TRADER_REQUIRE_RUNTIME_IDENTITY=1",
        "export TRADER_STRUCTURED_OUTPUT=1",
        "export M3_PRIORITY_TOKEN_FILE=",
        "export EXITMGR_ORDER_LOCK=",
    )
    for setting in required:
        assert setting in SCRIPT


def test_manual_slate_defaults_to_scheduled_watch_window():
    assert "WATCH=360" in SCRIPT
