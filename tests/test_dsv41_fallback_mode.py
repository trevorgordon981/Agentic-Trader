"""Public API contract; production-derived narrative omitted."""
import copy
import json
import socket
import unittest
from unittest.mock import patch
import pytest
from exitmgr import strategist as candidate

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline regression attempted network")
    monkeypatch.setattr(socket.socket, "connect", refuse)

def native_reply(intent):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'intents':[intent]})}}]}
INTENT={'underlying':'TEST','side':'debit','direction':'bullish','structure':'call debit spread',
        'target_dte':80,'intended_hold_days':8,'target_delta':0.60,'conviction':6,
        'allocation_pct_net_liq':5,'alpha':'Synthetic test only','thesis':'Synthetic test only'}

class TestFallbackMode(unittest.TestCase):
    def route(self,module,model,thinking='enabled',extra=None):
        body={'model':model,'thinking':thinking,'max_tokens':24000,'temperature':0.4,
              'messages':[{'role':'system','content':'test'},{'role':'user','content':'test'}],
              'chat_template_kwargs':{'thinking':thinking=='enabled','preserve_marker':'test'},
              'response_format':module._stage_a_response_format()}
        if extra: body.update(extra)
        before=copy.deepcopy(body);calls=[]
        def fake(endpoint,payload,*args,**kwargs):
            calls.append((endpoint,copy.deepcopy(payload)))
            if len(calls)==1: raise module.ModelRouteUnavailable('synthetic schema rejection')
            return {'choices':[{'message':{'content':'{"intents":[]}'}}]}
        with patch.dict('os.environ',{'TRADER_GLM_ONLY':'0'}),patch.object(module,'_post_json_one',fake):
            result=module._post_json(module._GLM_PRIMARY_ENDPOINT,body,10)
        self.assertEqual(before,body,'caller payload must not be mutated')
        return calls,before,result

    def test_native_and_spark_both_honor_on(self):
        calls,before,_=self.route(candidate,candidate._DSV41_PRIMARY_MODEL)
        self.assertEqual(len(calls),2)
        self.assertTrue(candidate._thinking_enabled(calls[0][1]))
        self.assertTrue(candidate._thinking_enabled(calls[1][1]))
        self.assertEqual(calls[0][1]['chat_template_kwargs'], {
            'enable_thinking': True, 'thinking_mode': 'thinking', 'preserve_marker': 'test'})
        self.assertEqual(calls[1][1]['chat_template_kwargs'], {
            'thinking': True, 'preserve_marker': 'test'})
        for _, body in calls:
            self.assertEqual(body['reasoning_effort'], 'high')
            self.assertNotIn('thinking', body)
            self.assertNotIn('enable_thinking', body)
        for key in ('messages','temperature','max_tokens','response_format'):
            self.assertEqual(calls[1][1][key],before[key])
        self.assertEqual(calls[1][1]['chat_template_kwargs']['preserve_marker'],'test')
        self.assertEqual(calls[1][1]['model'],candidate._DEEPSEEK_FALLBACK_MODEL)

    def test_glm_existing_thinking_on_preserved(self):
        calls,_,_=self.route(candidate,candidate._GLM_PRIMARY_MODEL)
        self.assertTrue(candidate._thinking_enabled(calls[0][1]))
        self.assertTrue(candidate._thinking_enabled(calls[1][1]))

    def test_explicit_off_remains_off(self):
        calls,_,_=self.route(candidate,candidate._DSV41_PRIMARY_MODEL,thinking='disabled')
        self.assertTrue(all(candidate._thinking_enabled(body) is False for _,body in calls))
        self.assertEqual(calls[0][1]['chat_template_kwargs']['thinking_mode'], 'chat')
        self.assertFalse(calls[0][1]['chat_template_kwargs']['enable_thinking'])
        self.assertFalse(calls[1][1]['chat_template_kwargs']['thinking'])
        self.assertTrue(all(body['reasoning_effort'] == 'none' for _,body in calls))

    def test_exact_only_does_not_fallback(self):
        calls=[]
        def fake(*args,**kwargs):
            calls.append(args)
            raise candidate.ModelRouteUnavailable('synthetic failure')
        body={'model':candidate._DSV41_PRIMARY_MODEL,'thinking':'enabled'}
        with patch.dict('os.environ',{'TRADER_GLM_ONLY':'1'}),patch.object(candidate,'_post_json_one',fake):
            with self.assertRaises(candidate.ModelRouteUnavailable):
                candidate._post_json(candidate._GLM_PRIMARY_ENDPOINT,body,10)
        self.assertEqual(len(calls),1)

    def test_non_native_route_not_rewritten(self):
        body={'model':'some-other-model','thinking':'enabled'}; calls=[]
        def fake(endpoint,payload,*args,**kwargs): calls.append(payload);return {'ok':True}
        with patch.object(candidate,'_post_json_one',fake):
            candidate._post_json('http://example.invalid/v1/chat/completions',body,10)
        self.assertEqual(calls,[body])

class TestSchemaPreserved(unittest.TestCase):
    def test_valid_shape_passes(self):
        candidate._validate_native_schema_response({'response_format':candidate._stage_a_response_format()},native_reply(INTENT))
    def test_known_explanatory_note_folded_verbatim(self):
        value=dict(INTENT,target_dte_note="Exact original note")
        reply=native_reply(value)
        original=reply["choices"][0]["message"]["content"]
        candidate._validate_native_schema_response({"response_format":candidate._stage_a_response_format()},reply)
        actual=json.loads(reply["choices"][0]["message"]["content"])["intents"][0]
        self.assertEqual(actual,dict(INTENT,thesis=INTENT["thesis"]+"\n\nExact original note"))
        self.assertEqual(reply["_trader_stage_a_normalization"]["original_content"],original)
    def test_execution_field_still_rejected(self):
        value=dict(INTENT,quantity=3)
        with self.assertRaises(candidate.ModelRouteUnavailable):
            candidate._validate_native_schema_response({'response_format':candidate._stage_a_response_format()},native_reply(value))
    def test_truncated_reply_still_rejected(self):
        reply=native_reply(INTENT);reply['choices'][0]['finish_reason']='length'
        with self.assertRaises(candidate.ModelRouteUnavailable):
            candidate._validate_native_schema_response({'response_format':candidate._stage_a_response_format()},reply)

if __name__=='__main__': unittest.main(verbosity=2)
