"""Public API contract; production-derived narrative omitted."""
import copy
from contextlib import nullcontext
import io
import json
import os
import socket
import urllib.error

import pytest

from exitmgr import native_text_recovery as recovery
from exitmgr import strategist

INTENT = dict(underlying="TEST", side="debit", direction="bullish",
              structure="call debit spread", target_dte=80, intended_hold_days=8,
              target_delta=0.6, conviction=6, allocation_pct_net_liq=5,
              alpha="Synthetic source-bound edge", thesis="Synthetic case and invalidation")


def document(**changes):
    return {"intents": [dict(INTENT, **changes)]}


def reply(doc=None, raw=None):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": raw if raw is not None else json.dumps(doc)}}]}


def body():
    return {"model": strategist._DSV41_PRIMARY_MODEL, "max_tokens": 1400,
            "temperature": 0.4, "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False, "thinking_mode": "chat"},
            "messages": [{"role": "system", "content": "Synthetic strict policy"},
                         {"role": "user", "content": "Synthetic supplied market evidence"}],
            "response_format": strategist._stage_a_response_format()}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network forbidden in native editorial tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", refuse)


def analysis(value):
    return recovery.analyze(body(), value, strategist._STAGE_A_JSON_SCHEMA,
                            strategist.parse_stage_a)


def run_recovery(tmp_path, original, corrected, **kwargs):
    calls = []
    def post_once(request, timeout):
        calls.append((copy.deepcopy(request), timeout))
        if isinstance(corrected, Exception):
            raise corrected
        return copy.deepcopy(corrected)
    result = recovery.recover(body(), original, strategist._STAGE_A_JSON_SCHEMA,
                              strategist.parse_stage_a, strategist._validate_native_schema_response,
                              post_once, recovery.time.monotonic() + 30,
                              runtime_before={"artifact_id": "test", "api_key": "not-recorded"},
                              directory=tmp_path, **kwargs)
    return result, calls


def test_1201_editorial_once_private_receipt_and_invariants(tmp_path):
    original = reply(document(thesis="x" * 1201))
    saved = copy.deepcopy(original)
    (result, revised_body), calls = run_recovery(tmp_path, original, reply(document(thesis="Concise case; invalidate if source fails.")))
    assert len(calls) == 1 and 0 < calls[0][1] <= 30
    assert original == saved
    for key in ("model", "max_tokens", "temperature", "reasoning_effort", "chat_template_kwargs", "response_format"):
        assert revised_body[key] == body()[key]
    assert revised_body["messages"][:2] == body()["messages"]
    assert result["_trader_text_recovery"]["edited_fields"] == [[0, "thesis"]]
    files = list(tmp_path.glob("*.json")); assert len(files) == 1
    receipt = json.loads(files[0].read_text())
    assert receipt["rejected_response"] == saved
    assert receipt["runtime_before"] == {"artifact_id": "test"}
    assert "not-recorded" not in files[0].read_text()
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert receipt["diagnostic"]["lengths"][1] == dict(intent_index=0,field="thesis",stage="baseline",observed=1201,minimum=1,maximum=1200)


def test_folded_length_preserves_notes_in_editorial_context(tmp_path):
    original = reply(document(thesis="x" * 1198, thesis_note="y"))
    a = analysis(original)
    assert a["eligible"] and a["lengths"][1]["observed"] == 1198
    assert a["lengths"][3]["observed"] == 1201
    (_, request), _ = run_recovery(tmp_path, original, reply(document(thesis="Short; y")))
    candidate = json.loads(request["messages"][-2]["content"])["intents"][0]
    assert candidate["thesis"] == "x" * 1198 + "\n\ny"
    assert "thesis_note" not in candidate


@pytest.mark.parametrize("changes", [{"thesis":""}, {"thesis":"x"*1201,"conviction":0},
    {"thesis":"x"*1201,"target_dte":5}, {"thesis":"x"*1201,"direction":"bearish"},
    {"thesis":"x"*1201,"quantity":1}, {"thesis":"x"*1201,"thesis_note":False}])
def test_other_contract_errors_not_recoverable(tmp_path, changes):
    assert not analysis(reply(document(**changes)))["eligible"]
    calls = []
    with pytest.raises(recovery.RecoveryError):
        recovery.recover(body(), reply(document(**changes)), strategist._STAGE_A_JSON_SCHEMA,
                         strategist.parse_stage_a, strategist._validate_native_schema_response,
                         lambda *a: calls.append(a), recovery.time.monotonic()+30, directory=tmp_path)
    assert calls == []


def test_duplicate_keys_are_not_editorial():
    raw=json.dumps(document(thesis="x"*1201)).replace('"conviction": 6', '"conviction": 6, "conviction": 6')
    assert not analysis(reply(raw=raw))["eligible"]


@pytest.mark.parametrize("field,value", [("underlying","OTHER"),("conviction",7),
                                        ("allocation_pct_net_liq",6),("alpha","Changed valid alpha")])
def test_revised_frozen_field_changes_rejected(tmp_path, field, value):
    with pytest.raises(recovery.RecoveryError, match="changed frozen field"):
        run_recovery(tmp_path, reply(document(thesis="x"*1201)), reply(document(**{field:value})))
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_second_attempt_still_invalid_no_third(tmp_path):
    with pytest.raises(recovery.RecoveryError, match="editorial attempt failed"):
        run_recovery(tmp_path, reply(document(thesis="x"*1201)), reply(document(thesis="x"*1300)))
    assert len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.parametrize("key", ["thesis_note", "target_dte_note"])
def test_editorial_added_notes_rejected_before_normalization(tmp_path, key):
    corrected = reply(document(**{key: "Added editorial note"}))
    with pytest.raises(recovery.RecoveryError, match="changed intent keys"):
        run_recovery(tmp_path, reply(document(thesis="x" * 1201)), corrected)
    receipts = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    rejected = next(item for item in receipts if item["attempt"] == 1)
    assert key in json.loads(rejected["rejected_response"]["choices"][0]["message"]["content"])["intents"][0]


def test_intent_count_and_order_frozen(tmp_path):
    original = {"intents":[dict(INTENT,thesis="x"*1201),dict(INTENT,underlying="OTHER")]}
    for corrected in [{"intents":[]}, {"intents":[dict(INTENT,underlying="OTHER"),dict(INTENT)]}]:
        with pytest.raises(recovery.RecoveryError, match="changed (intent count|frozen field)"):
            run_recovery(tmp_path, reply(original), reply(corrected))


def test_deadline_exhausted_no_second_request(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr(recovery.time,"monotonic",lambda:100.0)
    with pytest.raises(recovery.RecoveryError,match="deadline exhausted"):
        recovery.recover(body(),reply(document(thesis="x"*1201)),strategist._STAGE_A_JSON_SCHEMA,
            strategist.parse_stage_a,strategist._validate_native_schema_response,
            lambda *a:calls.append(a),100.0,directory=tmp_path)
    assert calls == [] and len(list(tmp_path.glob("*.json")))==1


def test_late_second_response_rejected(tmp_path,monkeypatch):
    now=[100.0];calls=[]
    monkeypatch.setattr(recovery.time,"monotonic",lambda:now[0])
    def late(*args):
        calls.append(args);now[0]=111.0;return reply(document())
    with pytest.raises(recovery.RecoveryError,match="deadline exhausted"):
        recovery.recover(body(),reply(document(thesis="x"*1201)),strategist._STAGE_A_JSON_SCHEMA,
            strategist.parse_stage_a,strategist._validate_native_schema_response,late,110.0,directory=tmp_path)
    assert len(calls)==1


class Response(io.BytesIO):
    headers={}
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


@pytest.fixture
def transport(monkeypatch,tmp_path):
    calls=[];replies=[]
    monkeypatch.setattr(strategist,"trader_glm_lease",lambda *a:nullcontext())
    monkeypatch.setattr(strategist.provenance,"priority_headers",lambda *a:{})
    original_receipt=recovery.receipt
    def private(*args,**kwargs):
        kwargs["directory"]=tmp_path
        return original_receipt(*args[:-1],**kwargs) if len(args)==7 else original_receipt(*args,**kwargs)
    monkeypatch.setattr(recovery,"receipt",private)
    monkeypatch.delenv("SLATE_POST_RETRIES",raising=False)
    def post(req,timeout):
        calls.append((req.full_url,json.loads(req.data),timeout))
        value=replies.pop(0)
        if isinstance(value,Exception):raise value
        return Response(json.dumps(value).encode())
    monkeypatch.setattr(strategist.urllib.request,"urlopen",post)
    return calls,replies


@pytest.mark.parametrize("doc", [document(thesis="x"*1200), {"intents":[]}])
def test_valid_boundary_and_decline_never_retried(transport,tmp_path,doc):
    calls,replies=transport;replies.append(reply(doc))
    strategist._post_json_one(strategist._GLM_PRIMARY_ENDPOINT,body(),10)
    assert len(calls)==1 and not list(tmp_path.glob("*.json"))


def test_real_request_boundary_one_same_primary_and_actual_provenance(transport,monkeypatch):
    calls,replies=transport;replies.extend([reply(document(thesis="x"*1201)),reply(document())])
    snap={"artifact_id":"same-runtime"}
    monkeypatch.setattr(strategist,"_runtime_snapshot_with_retry",lambda endpoint, **kwargs:dict(snap))
    monkeypatch.setattr(strategist.provenance,"request_identity",lambda **kw:kw)
    result,identity=strategist._post_json_one(strategist._GLM_PRIMARY_ENDPOINT,body(),10,return_identity=True)
    assert len(calls)==2 and calls[0][0]==calls[1][0]==strategist._GLM_PRIMARY_ENDPOINT
    assert identity["body"]==calls[1][1] and identity["before"]==identity["after"]==snap
    assert result["_trader_text_recovery"]["editorial_request_sha256"]==recovery.digest(calls[1][1])
    assert calls[1][2] <= calls[0][2]


def test_editorial_transport_failure_does_not_reenter_transport_retries(transport):
    calls,replies=transport;replies.extend([reply(document(thesis="x"*1201)),urllib.error.URLError("offline")])
    with pytest.raises(strategist.ModelRouteUnavailable,match="editorial attempt failed"):
        strategist._post_json_one(strategist._GLM_PRIMARY_ENDPOINT,body(),10,retries=5,backoff=0)
    assert len(calls)==2


def test_slow_body_absolute_deadline(monkeypatch):
    now=[100.0]
    monkeypatch.setattr(recovery.time,"monotonic",lambda:now[0])
    class Slow:
        def read(self,n):now[0]+=2;return b"x"
    with pytest.raises(recovery.RecoveryError,match="deadline exhausted"):
        recovery.read_response(Slow(),103.0)


def test_alpha_overflow_only_alpha_editable(tmp_path):
    (result,request),calls=run_recovery(tmp_path,reply(document(alpha="a"*601)),reply(document()))
    assert result["_trader_text_recovery"]["edited_fields"]==[[0,"alpha"]]
    assert len(calls)==1


def test_empty_diagnostic_records_zero_characters():
    a=analysis(reply(document(thesis="")))
    assert not a["eligible"]
    assert a["lengths"][-1]["field"]=="thesis" and a["lengths"][-1]["observed"]==0


def test_env_redirect_keeps_receipts_out_of_home(tmp_path,monkeypatch):
    destination=tmp_path/"private-rejections"
    monkeypatch.setenv("EXITMGR_SCHEMA_REJECTION_DIR",str(destination))
    record=recovery.receipt(body(),reply(document()),{},None,0,"rejected")
    assert os.path.dirname(record["path"])==str(destination)
    assert destination.stat().st_mode & 0o777==0o700


def test_receipt_failure_prevents_editorial_generation(transport,monkeypatch):
    calls,replies=transport;replies.append(reply(document(thesis="x"*1201)))
    def disk_full(*args,**kwargs):raise OSError("synthetic receipt unavailable")
    monkeypatch.setattr(recovery,"receipt",disk_full)
    with pytest.raises(strategist.ModelRouteUnavailable,match="receipt unavailable"):
        strategist._post_json_one(strategist._GLM_PRIMARY_ENDPOINT,body(),10)
    assert len(calls)==1


def test_accepted_editorial_still_requires_runtime_identity(transport,monkeypatch):
    calls,replies=transport;replies.extend([reply(document(thesis="x"*1201)),reply(document())])
    monkeypatch.setattr(strategist,"_runtime_snapshot_with_retry",lambda endpoint, **kwargs:{"artifact_id":"test"})
    monkeypatch.setattr(strategist.provenance,"identity_required",lambda:True)
    def mismatch(**kw):raise strategist.provenance.RuntimeIdentityError("synthetic runtime changed")
    monkeypatch.setattr(strategist.provenance,"request_identity",mismatch)
    with pytest.raises(strategist.provenance.RuntimeIdentityError,match="runtime changed"):
        strategist._post_json_one(strategist._GLM_PRIMARY_ENDPOINT,body(),10,return_identity=True)
    assert len(calls)==2
