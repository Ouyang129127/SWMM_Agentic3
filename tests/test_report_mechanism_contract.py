"""Reports must preserve each source classification, including empty candidate sets."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from test_report_agent import answer, example
from workflow_agents.reporting.narrative import _repair_feedback, make_prompt, validate, write_narrative


def unresolved_materials():
    materials=example(nodes=('Junction_A',))
    mechanisms=materials['events'][0]['assessment']['mechanism_assessments']
    mechanisms[1]['status']='needs_evidence'
    mechanisms.extend([
        {'mechanism_id':'M5','status':'not_applicable','reason':'未设置相关设施。'},
        {'mechanism_id':'M6','status':'needs_evidence','reason':'需补充蓄水过程。'},
        {'mechanism_id':'M2','status':'not_investigated','reason':'本轮未调查。'},
    ])
    return materials


class MechanismContractTests(unittest.IsolatedAsyncioTestCase):
    def test_contract_separates_candidate_from_other_unresolved_states(self):
        materials=unresolved_materials()
        second=copy.deepcopy(materials['events'][0])
        second['event_id']=second['event']['event_id']='Junction_A:E002'
        second['assessment']['mechanism_assessments'][1]['status']='candidate'
        materials['events'].append(second)
        before=copy.deepcopy(materials)
        prompt,_=make_prompt(materials)
        first_contract=prompt['event_output_contract']['Junction_A:E001']
        self.assertEqual(first_contract['supported_mechanism_ids'],['M1'])
        self.assertEqual(first_contract['candidate_mechanism_ids'],[])
        self.assertEqual(first_contract['source_status_by_mechanism']['M3'],'needs_evidence')
        self.assertEqual(prompt['event_output_contract']['Junction_A:E002']['candidate_mechanism_ids'],['M3'])
        self.assertEqual(materials,before)

    def test_absent_diagnosis_and_absent_events_have_explicit_empty_contracts(self):
        materials=example(nodes=('Junction_A',))
        materials['events'][0]['assessment']=None
        prompt,_=make_prompt(materials)
        contract=prompt['event_output_contract']['Junction_A:E001']
        self.assertEqual(contract,{'supported_mechanism_ids':[], 'candidate_mechanism_ids':[],
                                   'source_status_by_mechanism':{}})
        materials['events']=[]
        self.assertEqual(make_prompt(materials)[0]['event_output_contract'],{})

    async def test_repair_supplies_exact_empty_ids_and_removes_invented_prose(self):
        materials=unresolved_materials()
        prompt,facts=make_prompt(materials)
        bad=answer(prompt)
        bad['events'][0]['candidate_mechanism_ids']=['M3','M6']
        with self.assertRaisesRegex(ValueError,'status/coverage'):
            validate(bad,materials,facts,['Junction_A'])
        preserved=copy.deepcopy(bad)
        def respond(current):
            if 'repair' not in current:
                return copy.deepcopy(bad)
            repair=current['repair']
            self.assertEqual(repair['previous_response'],preserved)
            self.assertEqual(repair['event_output_contract'],prompt['event_output_contract'])
            mismatch=next(x for x in repair['mechanism_mismatches'] if x['field']=='candidate_mechanism_ids')
            self.assertEqual(mismatch['expected'],[])
            self.assertEqual(mismatch['actual'],['M3','M6'])
            prose=next(x for x in repair['mechanism_mismatches'] if x['field']=='candidate_influences')
            self.assertEqual(prose['expected'],[])
            fixed=answer(current)
            fixed['events'][0].update(candidate_mechanism_ids=[],candidate_influences=[])
            return fixed
        callback=AsyncMock(side_effect=respond)
        with tempfile.TemporaryDirectory() as temporary:
            result,_=await write_narrative(materials,['Junction_A'],Path(temporary),callback)
            first=json.loads((Path(temporary)/'narrative_attempt_1.json').read_text(encoding='utf-8'))
            self.assertEqual(first['raw_response'],preserved)
        self.assertEqual(callback.await_count,2)
        self.assertEqual(result['events'][0]['candidate_influences'],[])
        self.assertEqual(bad,preserved)

    def test_real_candidates_still_require_their_prose(self):
        prompt,_=make_prompt(example(nodes=('Junction_A',)))
        response=answer(prompt)
        response['events'][0]['candidate_influences']=[]
        feedback=_repair_feedback(prompt,response,ValueError('Missing candidate explanation'))
        self.assertEqual(feedback['event_output_contract']['Junction_A:E001']['candidate_mechanism_ids'],['M3'])
        self.assertEqual(feedback['mechanism_mismatches'],[
            {'event_id':'Junction_A:E001','field':'candidate_influences',
             'expected':'nonempty_paragraphs','actual':[]}])

    async def test_repeated_promotions_still_fail_and_never_autocorrect_output(self):
        materials=unresolved_materials()
        prompt,_=make_prompt(materials)
        callback=AsyncMock(side_effect=lambda _:answer(prompt))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError,'status/coverage'):
                await write_narrative(materials,['Junction_A'],Path(temporary),callback)
            last=json.loads((Path(temporary)/'narrative_attempt_3.json').read_text(encoding='utf-8'))
            self.assertEqual(last['status'],'failed')
        self.assertEqual(callback.await_count,3)

    def test_feedback_handles_malformed_response_and_duplicate_ids(self):
        prompt,_=make_prompt(unresolved_materials())
        for raw in ('invalid JSON',[],{'events':None},{'events':[None,{'event_id':[]}]}):
            with self.subTest(raw=raw):
                feedback=_repair_feedback(prompt,raw,ValueError('invalid response'))
                self.assertIn('event_output_contract',feedback)
                self.assertEqual(feedback['mechanism_mismatches'],[])
        bad=answer(prompt)
        bad['events'][0]['supported_mechanism_ids']=['M1','M1']
        feedback=_repair_feedback(prompt,json.dumps(bad),ValueError('duplicate mechanism'))
        self.assertTrue(any(x['field']=='supported_mechanism_ids' and x['expected']==['M1']
                            for x in feedback['mechanism_mismatches']))
