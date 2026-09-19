"""Public API contract; production-derived narrative omitted."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / 'exitmgr' / 'entry_contract.py'
spec = importlib.util.spec_from_file_location('notes_frozen_contract', CONTRACT)
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)
source = (HERE.parent / 'exitmgr' / 'strategist.py').read_text()
tree = ast.parse(source)
names = {'_STAGE_A_JSON_SCHEMA', '_normalize_stage_a_notes', '_validate_native_schema_response'}
selected = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in names) or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))]
ns = dict(json=json, EntryContractError=contract.EntryContractError, parse_stage_a=contract.parse_stage_a,
          ModelRouteUnavailable=type('ModelRouteUnavailable', (RuntimeError,), {}),
          provenance=types.SimpleNamespace(sha256=lambda x: hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()))
exec(compile(ast.Module(body=selected, type_ignores=[]), str(HERE.parent/'exitmgr'/'strategist.py'), 'exec'), ns)
normalize = ns['_normalize_stage_a_notes']
BASE = dict(underlying='CRWD', side='debit', direction='bullish', structure='call debit spread', target_dte=160,
            intended_hold_days=20, target_delta=0.55, conviction=7, allocation_pct_net_liq=2.0,
            alpha='Known catalyst', thesis='Original thesis.')
def doc(**changes):
    return {'intents': [dict(BASE, **changes)]}
try:
    import jsonschema
    HAVE_SCHEMA = True
except ImportError:
    HAVE_SCHEMA = False

class NotesTests(unittest.TestCase):
    def test_canonical_noop(self):
        d=doc(); result,edits=normalize(d); self.assertEqual(result,d); self.assertEqual(edits,[])
    def test_empty_stays_empty(self):
        self.assertEqual(normalize({'intents':[]}), ({'intents':[]}, []))
    def test_both_notes_fixed_order_values_and_types(self):
        d=doc(target_dte_note='  DTE exact. ', thesis_note='Do not trade unless confirmed.')
        saved=copy.deepcopy(d); result,edits=normalize(d); row=result['intents'][0]
        self.assertEqual(row['thesis'], BASE['thesis']+'\n\nDo not trade unless confirmed.\n\n  DTE exact. ')
        for key in BASE:
            if key!='thesis':
                self.assertEqual(row[key],BASE[key]); self.assertIs(type(row[key]), type(BASE[key]))
        self.assertEqual(d,saved); self.assertEqual(edits,[{'intent_index':0,'folded_keys':['thesis_note','target_dte_note']}])
    def test_single_note(self):
        self.assertEqual(normalize(doc(thesis_note='extra'))[0]['intents'][0]['thesis'], BASE['thesis']+'\n\nextra')
    def test_unknown_key_rejected(self):
        with self.assertRaises(contract.EntryContractError): normalize(doc(thesis_note='ok', strike=20))
    def test_unknown_top_level_rejected(self):
        with self.assertRaises(contract.EntryContractError): normalize(dict(doc(), note='bad'))
    def test_nonstring_notes_rejected(self):
        for val in [None,1,False,{},[]]:
            with self.subTest(val=val), self.assertRaises(contract.EntryContractError): normalize(doc(thesis_note=val))
    def test_empty_note_rejected(self):
        for val in ['', '  ', '\n']:
            with self.subTest(val=val), self.assertRaises(contract.EntryContractError): normalize(doc(thesis_note=val))
    def test_missing_thesis_rejected(self):
        d=doc(thesis_note='x'); del d['intents'][0]['thesis']
        with self.assertRaises(contract.EntryContractError): normalize(d)
    def test_thesis_exact_boundary(self):
        self.assertEqual(len(normalize(doc(thesis='x'*1197,thesis_note='y'))[0]['intents'][0]['thesis']),1200)
        with self.assertRaises(contract.EntryContractError): normalize(doc(thesis='x'*1198,thesis_note='y'))
    def test_existing_alpha_limit_enforced(self):
        normalize(doc(alpha='x'*600,thesis_note='y'))
        with self.assertRaises(contract.EntryContractError): normalize(doc(alpha='x'*601,thesis_note='y'))
    def test_existing_bad_thesis_not_rescued(self):
        with self.assertRaises(contract.EntryContractError): normalize(doc(thesis='',thesis_note='otherwise long'))
    def test_semantic_mismatch_rejected(self):
        for changes in [dict(direction='bearish'), dict(side='credit'), dict(target_dte=5), dict(target_dte=160.0), dict(conviction=True),dict(underlying='CRWD\n')]:
            with self.subTest(changes=changes),self.assertRaises(contract.EntryContractError): normalize(doc(thesis_note='note',**changes))
    def test_second_invalid_does_not_mutate_first(self):
        d=doc(thesis_note='note'); d['intents'].append(dict(BASE, conviction=0)); saved=copy.deepcopy(d)
        with self.assertRaises(contract.EntryContractError): normalize(d)
        self.assertEqual(d,saved)
    def test_nonfinite_not_accepted(self):
        with self.assertRaises(ValueError): normalize(doc(target_delta=float('inf'),thesis_note='note'))
    def test_too_many_intents(self):
        with self.assertRaises(contract.EntryContractError): normalize({'intents':[dict(BASE)]*4})
    def test_nonobjects_rejected(self):
        for d in [[], {'intents':{}}, {'intents':[None]}]:
            with self.subTest(d=d),self.assertRaises(contract.EntryContractError): normalize(d)

@unittest.skipUnless(HAVE_SCHEMA, 'jsonschema unavailable locally; rerun on trader host test interpreter')
class NativeBoundaryTests(unittest.TestCase):
    def body(self):
        return {'response_format':{'type':'json_schema','json_schema':{'name':'stage_a_intents','strict':True,'schema':copy.deepcopy(ns['_STAGE_A_JSON_SCHEMA'])}}}
    def response(self, raw): return {'choices':[{'finish_reason':'stop','message':{'content':raw}}]}
    def validate(self, body, result): return ns['_validate_native_schema_response'](body,result)
    def test_notes_before_schema_with_original_retained(self):
        raw=json.dumps(doc(thesis_note='note')); r=self.response(raw); self.validate(self.body(),r)
        self.assertEqual(r['_trader_stage_a_normalization']['original_content'],raw)
        self.assertEqual(json.loads(r['choices'][0]['message']['content'])['intents'][0]['thesis'],BASE['thesis']+'\n\nnote')
    def test_duplicate_rejected_before_transform(self):
        r=self.response(json.dumps(doc()).replace('"thesis":', '"thesis_note":"a","thesis_note":"b","thesis":'))
        saved=copy.deepcopy(r)
        with self.assertRaises(ns['ModelRouteUnavailable']): self.validate(self.body(),r)
        self.assertEqual(r,saved)
    def test_other_schema_names_not_repaired(self):
        body=self.body(); body['response_format']['json_schema']['name']='stage_b'
        with self.assertRaises(ns['ModelRouteUnavailable']): self.validate(body,self.response(json.dumps(doc(thesis_note='x'))))
    def test_changed_schema_not_repaired(self):
        body=self.body(); body['response_format']['json_schema']['schema']['description']='different contract'
        with self.assertRaises(ns['ModelRouteUnavailable']): self.validate(body,self.response(json.dumps(doc(thesis_note='x'))))
    def test_empty_unmodified_wire_and_no_normalization(self):
        r=self.response('{ "intents": [] }'); self.validate(self.body(),r)
        self.assertEqual(r['choices'][0]['message']['content'],'{ "intents": [] }'); self.assertNotIn('_trader_stage_a_normalization',r)
    def test_truncated_no_mutation(self):
        r=self.response(json.dumps(doc(thesis_note='x'))); r['choices'][0]['finish_reason']='length'; saved=copy.deepcopy(r)
        with self.assertRaises(ns['ModelRouteUnavailable']): self.validate(self.body(),r)
        self.assertEqual(r,saved)

if __name__=='__main__': unittest.main(verbosity=2)
