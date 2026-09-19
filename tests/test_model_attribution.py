from pathlib import Path
from types import SimpleNamespace

from exitmgr import approval


ROOT = Path(__file__).parents[1]


def ident(model_id, endpoint):
    return {"endpoint": endpoint, "runtime": {"model_id": model_id}}


GLM = ident(
    "glm-5.3-flash-candidate",
    "http://127.0.0.1:18080/v1/chat/completions",
)
DEEPSEEK = ident(
    "deepseek-v4-flash-0731",
    "http://127.0.0.1:8888/v1/chat/completions",
)


def test_glm_only_records_and_displays_glm():
    identity, source = approval.decisive_model_identity(GLM, "meta", GLM)
    assert identity is GLM and source == "meta"
    assert approval.model_display_name(identity, source) == "GLM-5.3-Flash"


def test_deepseek_fallback_records_and_displays_deepseek():
    identity, source = approval.decisive_model_identity(DEEPSEEK, "meta", DEEPSEEK)
    assert identity is DEEPSEEK and source == "meta"
    assert approval.model_display_name(identity, source) == "DeepSeek-V4-Flash"


def test_mixed_glm_stage_a_deepseek_stage_b_attributes_decisive_deepseek():
    identity, source = approval.decisive_model_identity(GLM, "meta", DEEPSEEK)
    assert identity is DEEPSEEK and source == "meta"
    assert approval.model_attribution_line(identity, source) == "_Model: DeepSeek-V4-Flash_\n"


def test_no_stage_b_falls_back_to_stage_a_identity():
    identity, source = approval.decisive_model_identity(GLM, "meta", None)
    assert identity is GLM and source == "meta"


def test_slack_proposal_uses_runtime_derived_label():
    idea = SimpleNamespace(
        underlying="TEST", direction="bullish", structure="call debit spread",
        target_dte=90, target_delta=0.60, est_debit_usd=250,
        conviction=7, thesis="test",
    )
    text = approval.format_proposal(
        idea, 5000, 500, 30, "BUY test",
        model_identity=DEEPSEEK, model_identity_source="meta")
    assert "_Model: DeepSeek-V4-Flash_" in text
    assert "GLM-5.3-Flash" not in text


def test_daily_and_continuous_paths_share_decisive_identity_rule():
    daily = (ROOT / "daily_recommend.py").read_text()
    trader = (ROOT / "exitmgr" / "trader.py").read_text()
    assert "approval.decisive_model_identity(" in daily
    assert "getattr(idea, \"_stage_b_identity\", None)" in daily
    assert "approval.model_attribution_line(model_identity, model_identity_source)" in daily
    assert "approval.decisive_model_identity(" in trader
    assert "getattr(idea, \"_stage_b_identity\", None)" in trader
    assert "model_identity=resolved.model_identity" in trader
    assert "model_identity_source=resolved.model_identity_source" in trader
