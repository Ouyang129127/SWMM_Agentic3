"""Report behavior across arbitrary events, invalid writing and failed delivery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
from PIL import Image

from workflow_agents.reporting.materials import SECTIONS, assert_sources_current, rainfall_metrics, saved_time_metrics, select_event_process
from workflow_agents.reporting.narrative import assemble_fixed_sections, expand, make_prompt, validate, write_narrative
from workflow_agents.reporting.pipeline import attach_display_report, check_html, generate_display_report
from workflow_agents.reporting.plots import make_plots
from workflow_agents.reporting.render import render
from workflow_agents.reporting.scene import build_scene
from workflow_agents.reporting.rain import build_rain
from workflow_agents.common.hashing import sha256_file


def example(nodes=('Junction_A','Junction_A','Outlet_B')):
    threshold={'max_area_m2':0.,'max_area_time':None,'final_area_m2':0.,
        'categories':{k:{'peak_area_m2':0.,'peak_time':None,'final_area_m2':0.} for k in ('road','green','building_adjacent')}}
    event=lambda n,i:{'node_id':n,'event_id':f'{n}:E{i:03}', 'start':'2024-01-01 00:05:00',
        'end':'2024-01-01 00:10:00','duration_minutes':5.,'estimated_volume_m3':2.,'peak_flooding_Ls':5.,'occurrence':i}
    events=[]
    for i,n in enumerate(nodes,1):
        e=event(n,i)
        events.append({'event':e,'event_id':e['event_id'],'node_id':n,'process':[
            {'time':t,'total_inflow_Ls':10.,'outflow_Ls':5.,'flooding_Ls':5.,'depth_m':1.}
            for t in ('2024-01-01 00:05:00','2024-01-01 00:10:00')],
            'source_composition':{'sources':[]},'incident_links':{},'surface_associations':[],
            'storage_evidence':None,'allow_simple_peak_balance':True,
            'assessment':{'process_explanation':'已有过程。','joint_assessment':{},'alternatives':'',
                'mechanism_assessments':[{'mechanism_id':'M1','status':'supported','reason':'来水集中。'},
                                        {'mechanism_id':'M3','status':'candidate','reason':'下游可能影响。'}]}})
    return {'revision':1,'task_id':'test','model_name':'test_model','run_id':'test_run','scope':'global',
        'scene':{'model_display_name':'示例模型','scenario_display_name':'示例方案',
        'simulation_start':'2024-01-01 00:00:00','simulation_end':'2024-01-01 00:15:00',
        'rain_end':'2024-01-01 00:10:00','run_date':'2026-10-07'},
        'rain':{'total_mm':10.,'peak_mm_h':60.,'shape':'单峰降雨'},
        'surface':{'classification_available':True,'max_saved_depth_m':.05,'max_depth_time':'2024-01-01 00:05:00',
                   'threshold_metrics':{'0.01':copy.deepcopy(threshold),'0.15':copy.deepcopy(threshold)},'process':[]},
        'overflow':{'node_count':len(set(nodes)),'event_count':len(nodes),'total_volume_m3':2.*len(nodes)},
        'events':events,'task_context':{},'diagnosis_overview':{},'verified_claims':[],
        'numeric_notes':[],'source_hashes':{}}


def answer(prompt):
    result={'intro':'依据本次保存结果说明降雨、地表变化与管网成因。',
        'sections':{k:['本次模拟结果按实际过程分析。'] for k in SECTIONS},
        'events':[{'event_id':eid,'process':['来水期间发生冒溢。'],
            'supported_mechanism_ids':['M1'],'supported_causes':['来水集中形成排水压力。'],
            'candidate_mechanism_ids':['M3'],'candidate_influences':['下游水位可能影响排水。']}
            for eid in prompt['expected_event_ids']]}
    for key,tokens in prompt.get('required_facts_by_section',{}).items():
        result['sections'][key].append('；'.join(tokens)+'。')
    for event,source in zip(result['events'],prompt['materials']['events']):
        event['process']=['；'.join(token for token in source['event'].values() if token)+'。']
    return result


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.materials=example();self.prompt,self.facts=make_prompt(self.materials)

    def tearDown(self):self.tmp.cleanup()

    def valid(self,response):return validate(response,self.materials,self.facts,['Junction_A','Outlet_B'])

    def process_row(self, links=('1', '6'), times=None, eid='10:E001', node='10'):
        times = times or ('2024-01-01 00:05:00', '2024-01-01 00:07:00', '2024-01-01 00:10:00')
        return {'event_id': eid, 'metric_name': 'local_process_series', 'value': {'series': [
            {'time': time, 'nodes': {node: {}}, 'links': {link: {} for link in links}}
            for time in times]}}

    def process_event(self):
        return {'event_id': '10:E001', 'node_id': '10', 'start': '2024-01-01 00:05:00',
                'peak_time': '2024-01-01 00:07:00', 'end': '2024-01-01 00:10:00'}

    def test_narrow_supplements_do_not_replace_complete_event_process(self):
        base = self.process_row()
        rows = [base, self.process_row(('5', '6', '7')), self.process_row(('6',))]
        original = copy.deepcopy(rows)
        process = select_event_process(rows, self.process_event(), {'1': {}, '6': {}})
        self.assertIs(process, base['value']['series'])
        self.assertEqual(rows, original)

    def test_process_selection_checks_every_sample_target_event_window_and_peak(self):
        incomplete = self.process_row()
        incomplete['value']['series'][1]['links'].pop('1')
        cases = [incomplete, self.process_row(node='21'), self.process_row(eid='10:E002'),
                 self.process_row(times=('2024-01-01 00:07:00', '2024-01-01 00:10:00')),
                 self.process_row(times=('2024-01-01 00:05:00', '2024-01-01 00:10:00'))]
        for row in cases:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'Missing complete.*10:E001'):
                select_event_process([row], self.process_event(), {'1': {}, '6': {}})

    def test_complete_supplement_can_supply_missing_event_process(self):
        supplement = self.process_row()
        rows = [self.process_row(('6',)), supplement]
        self.assertIs(select_event_process(rows, self.process_event(), {'1': {}, '6': {}}),
                      supplement['value']['series'])

    def test_all_events_including_repeated_node_are_required(self):
        r=answer(self.prompt);self.valid(r);r['events'].pop()
        with self.assertRaisesRegex(ValueError,'every event'):self.valid(r)

    def test_duplicate_event_is_rejected(self):
        r=answer(self.prompt);r['events'][1]=copy.deepcopy(r['events'][0])
        with self.assertRaisesRegex(ValueError,'every event'):self.valid(r)

    def test_supported_and_candidate_cannot_be_upgraded(self):
        r=answer(self.prompt);r['events'][0]['supported_mechanism_ids'].append('M3')
        with self.assertRaisesRegex(ValueError,'status/coverage'):self.valid(r)

    def test_candidate_must_retain_uncertainty(self):
        r=answer(self.prompt);r['events'][0]['candidate_influences']=['下游顶托是主要原因。']
        with self.assertRaisesRegex(ValueError,'explicitly possible'):self.valid(r)

    def test_unconfirmed_candidate_cannot_be_rewritten_as_a_small_effect(self):
        r=answer(self.prompt);r['events'][0]['candidate_influences']=['下游可能形成顶托，顶托作用可能有限。']
        with self.assertRaisesRegex(ValueError,'limited-effect'):self.valid(r)

    def test_full_pipe_cannot_be_rewritten_as_permanently_constant_flow(self):
        r=answer(self.prompt);r['events'][0]['supported_causes']=['满流后出流无法继续增加，节点水位升高。']
        with self.assertRaisesRegex(ValueError,'constant outflow'):self.valid(r)

    def test_unconnected_nodes_cannot_be_presented_as_a_pipe_chain(self):
        r=answer(self.prompt);r['sections']['causes']=['Junction_A—Outlet_B构成连续管网链。']
        with self.assertRaisesRegex(ValueError,'branching topology'):self.valid(r)

    def test_fabricated_numeric_claim_and_unknown_token_rejected(self):
        for sentence in ('持续了99分钟。','面积为[[F9999]]。'):
            r=answer(self.prompt);r['intro']=sentence
            with self.assertRaises(ValueError):self.valid(r)

    def test_known_fact_references_expand_without_exposing_tokens(self):
        r=answer(self.prompt);r['intro']=self.prompt['materials']['rain']['total_mm']+'。'
        self.valid(r)
        media=self.images();p=self.root/'report.html'
        render(self.materials,r,self.facts,media,p,'now');checks=check_html(p,self.materials)
        self.assertEqual(checks['events'],3);self.assertNotIn('[[F',p.read_text(encoding='utf-8'))
        self.assertIn('10.0毫米',p.read_text(encoding='utf-8'))

    def test_direct_subcatchment_identifiers_reach_facts_and_rendered_report(self):
        materials=example(nodes=('Junction_A',))
        materials['events'][0]['direct_subcatchments']=[
            {'subcatchment_id':'2','outlet_node_id':'Junction_A'},
            {'subcatchment_id':'SC_7','outlet_node_id':'Junction_A'}]
        prompt,facts=make_prompt(materials)
        token=prompt['materials']['events'][0]['direct_subcatchment_connection']
        self.assertEqual(expand(token,facts),'子汇水区2、子汇水区SC_7直接汇入节点Junction_A')
        response=answer(prompt)
        response['events'][0]['process'].append(token+'。')
        validate(response,materials,facts,['Junction_A'])
        path=self.root/'subcatchments.html'
        render(materials,response,facts,self.images(),path,'now')
        check_html(path,materials)
        self.assertIn('子汇水区2、子汇水区SC_7直接汇入节点Junction_A',path.read_text(encoding='utf-8'))

    def test_missing_direct_subcatchment_fact_is_reported_for_repair(self):
        materials=example(nodes=('Junction_A',))
        materials['events'][0]['direct_subcatchments']=[
            {'subcatchment_id':'2','outlet_node_id':'Junction_A'}]
        prompt,facts=make_prompt(materials)
        response=answer(prompt)
        with self.assertRaisesRegex(ValueError,'Missing direct-subcatchment connection'):
            validate(response,materials,facts,['Junction_A'])

    def test_absent_or_empty_direct_subcatchments_preserve_existing_facts(self):
        baseline=example(nodes=('Junction_A',))
        old_prompt,old_facts=make_prompt(baseline)
        baseline['events'][0]['direct_subcatchments']=[]
        prompt,facts=make_prompt(baseline)
        self.assertEqual(facts,old_facts)
        self.assertEqual(prompt['required_facts_by_event'],{})
        self.assertNotIn('direct_subcatchment_connection',prompt['materials']['events'][0])
        validate(answer(prompt),baseline,facts,['Junction_A'])

    def images(self):
        p=self.root/'image.png';Image.new('RGB',(12,12),'white').save(p)
        return {k:{'path':str(p),'caption':'实际图像'} for k in ('rainfall','surface_maps','surface_process','surface_stages')}

    def test_zero_overflow_still_renders_six_sections(self):
        m=example(());prompt,facts=make_prompt(m);r=answer(prompt)
        validate(r,m,facts,[]);p=self.root/'zero.html'
        render(m,r,facts,self.images(),p,'now')
        self.assertEqual(check_html(p,m)['events'],0)

    def test_complete_fact_cannot_be_embedded_as_a_scalar(self):
        r=answer(self.prompt);r['intro']='总雨量为'+self.prompt['materials']['rain']['total_mm']+'。'
        with self.assertRaisesRegex(ValueError,'standalone clause'):self.valid(r)

    def test_local_rise_and_plateau_are_available_without_all_metric_sentences(self):
        m=example(('Junction_A',))
        m['events'][0]['process'].insert(1,{'time':'2024-01-01 00:07:00',
            'total_inflow_Ls':12.,'outflow_Ls':5.,'flooding_Ls':7.,'depth_m':1.})
        prompt,facts=make_prompt(m)
        rows=prompt['materials']['events'][0]['process']
        self.assertEqual(len(rows),3)
        comparison=facts[rows[1]['flow_comparison'][2:-2]]
        self.assertIn('00:07',comparison);self.assertIn('12.0升/秒',comparison)
        self.assertNotIn('节点水深',comparison);self.assertNotIn('冒溢流量',comparison)
        self.assertIn('节点水深',facts[rows[1]['depth'][2:-2]])

    def test_current_event_cannot_use_another_event_numeric_fact(self):
        r=answer(self.prompt)
        r['events'][0]['process'].append(self.prompt['materials']['events'][1]['event']['summary']+'。')
        with self.assertRaisesRegex(ValueError,'another event'):self.valid(r)

    def test_peak_comparison_and_trend_share_the_correct_event_interval(self):
        m=example(('Up',));source=m['events'][0]
        source['event'].update(peak_time='2024-01-01 00:07:00',peak_flooding_Ls=8.)
        source['incident_links']={'Pipe':{'from_node':'Up','to_node':'Down'}}
        source['process'].insert(0,{'time':'2024-01-01 00:04:00','total_inflow_Ls':1.,
            'outflow_Ls':1.,'flooding_Ls':0.,'depth_m':.1})
        source['process'].insert(2,{'time':'2024-01-01 00:07:00','total_inflow_Ls':12.,
            'outflow_Ls':4.,'flooding_Ls':8.,'depth_m':1.})
        for row in source['process']:row['outgoing_by_link_Ls']={'Pipe':row['outflow_Ls']}
        prompt,facts=make_prompt(m);item=prompt['materials']['events'][0]
        peak=facts[item['event']['peak'][2:-2]]
        for value in ('Up','Pipe','00:07','12.0升/秒','4.0升/秒','8.0升/秒'):
            self.assertIn(value,peak)
        self.assertNotIn('00:04',peak)
        anchor=item['process_comparison_anchor']
        self.assertEqual(anchor['fact'],item['process'][1]['flow_comparison'])
        self.assertEqual(anchor['inflow_direction'],'增加')
        self.assertEqual(anchor['outflow_direction'],'减少')
        self.assertTrue(item['peak_includes_flow_comparison'])
        source['process'].pop(2)
        prompt,facts=make_prompt(m);item=prompt['materials']['events'][0]
        self.assertFalse(item['peak_includes_flow_comparison'])
        self.assertNotIn('Pipe',facts[item['event']['peak'][2:-2]])
        self.assertNotIn('process_comparison_anchor',item)

    def test_teaching_fact_tokens_cannot_escape_into_current_report(self):
        r=answer(self.prompt);r['events'][0]['process'].append('[[EX001]]。')
        with self.assertRaisesRegex(ValueError,'Teaching example fact'):self.valid(r)

    def test_internal_assessment_patch_is_rejected_but_natural_possible_language_passes(self):
        r=answer(self.prompt);r['events'][0]['candidate_influences']=['下游水位可能影响排水，但尚未证实。']
        with self.assertRaisesRegex(ValueError,'reader-facing'):self.valid(r)
        r['events'][0]['candidate_influences']=['下游高水位可能限制向下游排水。']
        self.valid(r)

    def test_overview_cannot_end_with_internal_judgment_disclaimers(self):
        for text in ('下游高水位可能影响排水，但当前只能作为候选影响，不能据此确定主因。',
                     '来水与输水约束共同作用，其作用大小尚不明确。',
                     '下游高水位目前只能保留为一种解释。'):
            with self.subTest(text=text):
                r=answer(self.prompt);r['sections']['causes']=[text]
                with self.assertRaisesRegex(ValueError,'judgment-status'):self.valid(r)
        r=answer(self.prompt);r['sections']['causes']=[
            '集中来水与局部输水约束共同造成节点冒溢。',
            '下游高水位可能进一步影响管段排水，与局部输水约束共同作用。']
        self.valid(r)

    def test_overview_explains_causes_without_repeating_statistics(self):
        r=answer(self.prompt);r['sections']['causes']=[self.prompt['materials']['rain']['total_mm']+'。来水集中。']
        with self.assertRaisesRegex(ValueError,'without repeated quantitative'):self.valid(r)

    def test_downstream_influence_cannot_reverse_the_reported_discharge_direction(self):
        r=answer(self.prompt);r['sections']['causes']=['下游高水位可能形成顶托，使向上游方向的排水更加困难。']
        with self.assertRaisesRegex(ValueError,'toward downstream'):self.valid(r)
        r['sections']['causes']=['下游高水位可能使上游节点向下游排水更加困难。']
        self.valid(r)

    def test_node_title_is_rendered_and_checked_for_unbound_numbers(self):
        r=answer(self.prompt);r['events'][0]['title']='Junction_A：来水集中，排水压力增加'
        self.valid(r)
        p=self.root/'titled.html';render(self.materials,r,self.facts,self.images(),p,'now')
        self.assertIn('Junction_A：来水集中，排水压力增加',p.read_text(encoding='utf-8'))
        r['events'][0]['title']='Junction_A：持续99分钟'
        with self.assertRaisesRegex(ValueError,'Unbound numeric'):self.valid(r)

    def test_numeric_flow_mismatch_does_not_block_head_description(self):
        self.materials['events'][0]['allow_simple_peak_balance']=False
        r=answer(self.prompt);r['events'][0]['supported_causes']=['水头等于井口高程时发生冒溢。']
        self.valid(r)
        r['events'][0]['process'].append('入流与出流差值等于冒溢流量。')
        with self.assertRaisesRegex(ValueError,'flow equality'):self.valid(r)

    def test_process_metric_dump_is_rejected_but_selected_comparison_passes(self):
        r=answer(self.prompt)
        snapshot=self.prompt['materials']['events'][0]['process'][0]
        r['events'][0]['process'].append('；'.join(snapshot[k] for k in ('flow_comparison','depth','flooding'))+'。')
        with self.assertRaisesRegex(ValueError,'too many sampled metrics'):self.valid(r)
        r['events'][0]['process'][-1]=snapshot['flow_comparison']+'。来水增加了节点的排水压力。'
        self.valid(r)

    def test_peak_comparison_cannot_move_the_overflow_onset(self):
        m=example(('Junction_A',));m['events'][0]['event']['peak_time']='2024-01-01 00:07:00'
        m['events'][0]['process'][0]['time']='2024-01-01 00:04:00'
        row=copy.deepcopy(m['events'][0]['process'][0]);row['time']='2024-01-01 00:07:00'
        m['events'][0]['process'].insert(1,row)
        prompt,facts=make_prompt(m);r=answer(prompt)
        peak=prompt['materials']['events'][0]['process'][1]['flow_comparison']
        r['events'][0]['process'].append(peak+'。节点水位升高并开始冒溢。')
        with self.assertRaisesRegex(ValueError,'after onset'):validate(r,m,facts,['Junction_A'])
        r['events'][0]['process'][-1]=peak+'。节点处于冒溢高峰。'
        validate(r,m,facts,['Junction_A'])

    def test_simultaneous_onsets_cannot_be_described_as_sequential(self):
        r=answer(self.prompt);r['events'][2]['process'].append('Outlet_B紧随Junction_A之后发生冒溢。')
        with self.assertRaisesRegex(ValueError,'same saved time'):self.valid(r)

    def test_downstream_context_distinguishes_earlier_overflow_from_concurrent_water_level(self):
        m=example(('Up','Down'))
        m['events'][0]['event'].update(start='2024-01-01 00:10:00',end='2024-01-01 00:15:00')
        m['events'][0]['incident_links']={'Pipe':{'from_node':'Up','to_node':'Down'}}
        prompt,facts=make_prompt(m);r=answer(prompt)
        self.assertIn('已在本事件开始前结束',prompt['materials']['events'][0]['downstream_event_context'][0]['relation'])
        r['events'][0]['candidate_influences']=['下游Down尚未冒溢，其高水位可能影响排水。']
        with self.assertRaisesRegex(ValueError,'already ended'):validate(r,m,facts,['Up','Down','Pipe'])
        r['events'][0]['candidate_influences']=['下游Down的同期高水位可能限制向下游排水。']
        validate(r,m,facts,['Up','Down','Pipe'])

    def test_html_and_internal_labels_are_rejected(self):
        for text in ('<script>危险</script>','M1导致冒溢。'):
            r=answer(self.prompt);r['intro']=text
            with self.assertRaises(ValueError):self.valid(r)

    async def test_bad_model_response_receives_one_repair(self):
        invalid=answer(self.prompt);invalid['events']=[]
        callback=AsyncMock(side_effect=[invalid,answer(self.prompt)])
        r,_=await write_narrative(self.materials,['Junction_A','Outlet_B'],self.root,callback)
        self.assertEqual(len(r['events']),3);self.assertEqual(callback.await_count,2)
        self.assertIn('repair',callback.call_args.args[0])

    async def test_network_failure_is_preserved_without_fallback_prose(self):
        callback=AsyncMock(side_effect=RuntimeError('service unavailable'))
        with self.assertRaisesRegex(RuntimeError,'service unavailable'):
            await write_narrative(self.materials,[],self.root,callback)
        self.assertEqual(callback.await_count,1)
        self.assertEqual(json.loads((self.root/'narrative_attempt_1.json').read_text())['status'],'failed')

    async def test_scene_uses_source_materials_and_keeps_raw_model_audit(self):
        response=answer(self.prompt)
        response['sections']['scene']=['模型随意写入9999小时和9999毫米。']
        callback=AsyncMock(return_value=response)
        result,facts=await write_narrative(self.materials,['Junction_A','Outlet_B'],self.root,callback)
        text=''.join(expand(p,facts) for p in result['sections']['scene'])
        self.assertNotIn('9999',text)
        self.assertIn('累计雨量10毫米',text)
        self.assertIn('雨停后5分钟',text)
        self.assertIn('9999',response['sections']['scene'][0])
        audit=json.loads((self.root/'narrative_attempt_1.json').read_text(encoding='utf-8'))
        self.assertIn('9999',audit['raw_response']['sections']['scene'][0])
        self.assertEqual(audit['fixed_section_sources']['scene']['source'],'program_template')

    def test_scene_preserves_cross_midnight_and_actual_post_rain_duration(self):
        m=example(('Node',))
        m['scene'].update(model_display_name='UrbanDrainage scenario family',scenario_display_name='基准方案',
            simulation_start='2024-01-01 23:00:00',simulation_end='2024-01-02 04:00:00',
            rain_start='2024-01-01 23:00:00',rain_end='2024-01-02 01:00:00')
        m['rain'].update(total_mm=50.,duration_min=120.,shape='双峰降雨')
        paragraph=build_scene(m)[0]
        for phrase in ('UrbanDrainage区域','历时2小时','累计雨量50毫米','双峰降雨',
                       '降雨于次日01:00结束','计算继续至次日04:00','雨停后3小时'):
            self.assertIn(phrase,paragraph)
        self.assertNotIn('2024-01',paragraph)
        m['scene']['simulation_end']=m['scene']['rain_end']
        self.assertNotIn('雨停后',build_scene(m)[0])

    def test_single_double_and_multiple_rain_peaks_are_distinguished(self):
        for values,shape in (([0,20,0],'单峰降雨'),([0,20,0,30,0],'双峰降雨'),
                             ([0,20,0,30,0,10,0],'多峰降雨')):
            with self.subTest(shape=shape):
                times=pd.date_range('2024-01-01',periods=len(values),freq='5min')
                path=self.root/'rain_peaks.csv'
                pd.DataFrame({'timestamp':times,'value':values}).to_csv(path,index=False)
                metrics,_frame,_cumulative=rainfall_metrics(path,str(times[-1]))
                self.assertEqual(metrics['shape'],shape)

    async def test_fixed_rain_keeps_actual_interval_and_raw_model_response(self):
        times = pd.date_range('2024-01-01', periods=7, freq='10min')
        path = self.root/'rain_fixed.csv'
        pd.DataFrame({'timestamp':times,'value':[0.,10.,90.,20.,10.,0.,0.]}).to_csv(path,index=False)
        m=example()
        m['rain']=rainfall_metrics(path,str(times[-1]))[0]
        prompt, _=make_prompt(m)
        response=answer(prompt)
        response['sections']['rain']=['最大5分钟雨强9999毫米/小时。']
        result,facts=await write_narrative(m,['Junction_A','Outlet_B'],self.root,AsyncMock(return_value=response))
        prose=''.join(expand(p,facts) for p in result['sections']['rain'])
        self.assertIn('最大10分钟平均雨强约为90.00毫米/小时',prose)
        self.assertIn('中前段形成集中雨峰',prose)
        self.assertNotIn('9999',prose)
        audit=json.loads((self.root/'narrative_attempt_1.json').read_text(encoding='utf-8'))
        self.assertIn('9999',audit['raw_response']['sections']['rain'][0])
        self.assertEqual(audit['fixed_section_sources']['rain']['source'],'program_template')

    def test_surface_basis_is_first_and_assembly_preserves_results_without_duplicates(self):
        response=answer(self.prompt)
        opening=self.prompt['fixed_section_openings']['surface']
        response['sections']['surface'].append(opening)
        previous=copy.deepcopy(response)
        result=assemble_fixed_sections(response,self.prompt)
        self.assertEqual(result['sections']['surface'][0],opening)
        self.assertEqual(result['sections']['surface'][1:],previous['sections']['surface'][:-1])
        self.assertEqual(assemble_fixed_sections(result,self.prompt),result)
        self.assertEqual(response,previous)
        text=expand(opening,self.facts)
        self.assertIn('积水深度的风险',text)
        self.assertIn('北京市水务局',text)
        self.assertIn('15、27和40厘米',text)
        self.assertIn('轻微积水',text)
        self.valid(result)

    def test_rain_shape_evolution_and_short_uniform_window_are_not_fabricated(self):
        for values,shape in (([0,20,0,30,0],'双峰'),([0,20,0,30,0,10,0],'多峰')):
            times=pd.date_range('2024-01-01 23:45:00',periods=len(values),freq='5min')
            path=self.root/'rain_shape.csv'
            pd.DataFrame({'timestamp':times,'value':values}).to_csv(path,index=False)
            m=example();m['scene']['rain_start']=str(times[0])
            m['rain']=rainfall_metrics(path,str(times[-1]))[0]
            text=''.join(build_rain(m))
            self.assertIn(shape,text)
            self.assertNotIn('随后逐步减弱',text)
            self.assertIn('次日',text)
        pd.DataFrame({'timestamp':['2024-01-01 00:00:00'],'value':[60.]}).to_csv(path,index=False)
        m['scene']['rain_start']='2024-01-01 00:00:00'
        m['rain']=rainfall_metrics(path,'2024-01-01 00:20:00')[0]
        text=''.join(build_rain(m))
        self.assertIn('连续20分钟',text)
        self.assertIn('100%',text)
        self.assertNotIn('集中在较短时间内',text)
        m['rain']['total_mm']=0.
        self.assertEqual(build_rain(m),['本次模拟期间没有降雨。'])

    def test_irregular_rain_intervals_and_rolling_30_minutes(self):
        p=self.root/'rain.csv'
        pd.DataFrame({'timestamp':['2024-01-01 00:00:00','2024-01-01 00:10:00','2024-01-01 00:30:00','2024-01-01 00:50:00'],
                      'value':[0.,60.,30.,0.]}).to_csv(p,index=False)
        metrics,_,_=rainfall_metrics(p,'2024-01-01 00:50:00')
        self.assertAlmostEqual(metrics['total_mm'],30.);self.assertAlmostEqual(metrics['window_mm'],25.)
        self.assertEqual(metrics['window_start'],'2024-01-01 00:10:00')

    def test_short_and_zero_rain(self):
        p=self.root/'rain.csv'
        for rate,total in ((60.,20.),(0.,0.)):
            pd.DataFrame({'timestamp':['2024-01-01 00:00:00','2024-01-01 00:20:00'],'value':[rate,0.]}).to_csv(p,index=False)
            metrics,_,_=rainfall_metrics(p,'2024-01-01 00:20:00')
            self.assertEqual(metrics['window_minutes'],20.);self.assertEqual(metrics['window_mm'],total)

    def test_source_mutation_is_rejected(self):
        p=self.root/'source.json';p.write_text('original')
        materials={'source_hashes':{str(p):sha256_file(p)}};assert_sources_current(materials)
        p.write_text('changed')
        with self.assertRaisesRegex(ValueError,'source changed'):assert_sources_current(materials)

    def test_last_rain_interval_is_included_without_terminal_zero(self):
        p=self.root/'rain.csv'
        pd.DataFrame({'timestamp':['2024-01-01 00:00:00'],'value':[60.]}).to_csv(p,index=False)
        metrics,frame,cumulative=rainfall_metrics(p,'2024-01-01 00:20:00')
        self.assertEqual(metrics['total_mm'],20.);self.assertEqual(len(frame),2)
        self.assertEqual(cumulative[-1],20.)

    def test_surface_facts_bind_time_and_area_in_one_reference(self):
        m=example(())
        m['surface']['threshold_metrics']['0.01'].update(rain_end_area_m2=828.,
            rain_end_sample_time='2024-01-01 00:10:00',final_area_m2=819.)
        prompt,facts=make_prompt(m)
        from workflow_agents.reporting.narrative import expand
        rain_end=expand(prompt['materials']['surface']['general']['rain_end'],facts)
        final=expand(prompt['materials']['surface']['general']['final'],facts)
        self.assertIn('00:10',rain_end);self.assertIn('828',rain_end);self.assertNotIn('819',rain_end)
        self.assertIn('00:15',final);self.assertIn('819',final)

    def test_microsecond_and_nanosecond_indices_select_same_rain_end(self):
        times=pd.date_range('2024-01-01',periods=85,freq='5min')
        for unit in ('us','ns'):
            steps,nearest=saved_time_metrics(times.as_unit(unit),'2024-01-01 04:00:00')
            self.assertEqual(nearest,48);self.assertEqual(steps[0],5.)
            self.assertEqual(steps.sum(),420.)

    def test_lateral_source_uses_display_name_instead_of_internal_field(self):
        m=example();m['events'][0]['source_composition']['sources']=[{'source_id':'aggregate_lateral','event_volume_share':1.}]
        prompt,facts=make_prompt(m)
        source=prompt['materials']['events'][0]['source_composition']['sources'][0]
        self.assertEqual(source['source_name'],'侧向入流')
        self.assertFalse(any('aggregate_lateral' in text for text in facts.values()))

    def test_rain_peak_relation_is_required_in_overflow_prose(self):
        m=example();m['overflow'].update(first_start='2024-01-01 00:05:00',last_end='2024-01-01 00:10:00')
        m['rain'].update(peak_start='2024-01-01 00:00:00',peak_end='2024-01-01 00:10:00',peak_interval_min=10.)
        prompt,facts=make_prompt(m);r=answer(prompt);validate(r,m,facts,['Junction_A','Outlet_B'])
        token=prompt['materials']['overflow']['rain_peak_relation'];self.assertIn('区间内',facts[token[2:-2]])
        r['sections']['overflow']=['峰值附近发生冒溢。']
        with self.assertRaisesRegex(ValueError,'rain-peak relation'):validate(r,m,facts,['Junction_A','Outlet_B'])

    def test_attention_location_and_duration_cannot_be_omitted(self):
        m=example();m['events'][0]['surface_associations']=[{'land_type':'道路','max_saved_depth_m':.16,
            'max_depth_time':'2024-01-01 00:05:00','final_depth_m':0.,'meaning':'注入网格',
            'attention_episodes':[{'first_saved':'2024-01-01 00:05:00','last_saved':'2024-01-01 00:05:00',
                'continuous_episode_upper_estimate_min':10.,'boundary_censored':False}]}]
        prompt,facts=make_prompt(m);r=answer(prompt);validate(r,m,facts,['Junction_A','Outlet_B'])
        token=prompt['required_facts_by_section']['surface'][0]
        self.assertIn('不超过10分钟',facts[token[2:-2]]);self.assertIn('道路',facts[token[2:-2]])
        r['sections']['surface']=['短时积水随后消退。']
        with self.assertRaisesRegex(ValueError,'location/duration'):validate(r,m,facts,['Junction_A','Outlet_B'])

    async def test_full_pipeline_and_unchanged_reuse(self):
        state={'task_id':'test','revision':1,'diagnosis_hash':'hash'}
        (self.root/'task.json').write_text(json.dumps(state))
        with patch('workflow_agents.reporting.pipeline.prepare_materials',return_value=(self.materials,{'known_ids':['Junction_A','Outlet_B']})), \
             patch('workflow_agents.reporting.pipeline.make_plots',return_value=(self.images(),[])), \
             patch('workflow_agents.reporting.pipeline.verify_task'):
            callback=AsyncMock(side_effect=answer)
            result=await attach_display_report(self.root,self.root,state,callback)
            self.assertEqual(result['status'],'completed',result);self.assertTrue(Path(result['html_file']).is_file())
            repeat=await attach_display_report(self.root,self.root,state,callback)
            self.assertTrue(repeat['reused']);self.assertEqual(callback.await_count,1)

    async def test_new_revision_failure_does_not_present_previous_html_as_current(self):
        old={'status':'completed','revision':1,'html_file':'old.html'}
        state={'task_id':'test','revision':2,'display_report':old,'display_report_file':'old.html','display_report_revision':1}
        callback=AsyncMock()
        with patch('workflow_agents.reporting.pipeline.prepare_materials',side_effect=ValueError('stale evidence')):
            result=await attach_display_report(self.root,self.root,state,callback)
        self.assertEqual(result['status'],'failed');self.assertNotIn('display_report_file',state)
        self.assertEqual(state['previous_display_report'],old);callback.assert_not_called()

    def test_zero_events_maps_no_attention_and_no_road_classification(self):
        m=example(());m['surface']['classification_available']=False
        times=pd.date_range('2024-01-01',periods=4,freq='5min');grid=np.arange(1,7).reshape(2,3)
        depth=np.zeros((4,2,3));depth[1,0,0]=.05
        d={'grid':grid,'flow':np.ones((2,3),bool),'road':np.zeros((2,3),bool),'green':np.ones((2,3),bool),
           'building':np.zeros((2,3),bool),'valid':np.ones((2,3),bool),'classification_available':False,
           'depth':depth,'times':times,'areas':{.01:np.array([0,9,0,0]),.15:np.zeros(4)},'max_depth':depth.max(axis=(1,2)),
           'phase_indices':[0,1,3],'rain_frame':pd.DataFrame({'timestamp':times,'value':[60,60,0,0]}),
           'cumulative_rain':[0,5,10,10],'mapping':pd.DataFrame(columns=['node_id','cell_col','cell_row']),'gif':None}
        media,layout=make_plots(m,d,self.root/'plots')
        self.assertEqual(len(media),4);self.assertEqual(layout,[])
        for image in media.values():
            with Image.open(image['path']) as im:im.verify()

    async def test_public_report_resolves_active_task_and_preserves_question(self):
        import workflow_agents.orchestration.agents as main
        with patch('workflow_agents.orchestration.investigation_flow.load_active_task',return_value={'task_id':'bound','state':'preliminary_delivered'}), \
             patch('workflow_agents.orchestration.agent_actions._investigation_action',new_callable=AsyncMock,return_value='result') as dispatch:
            await main.ReportAgent('model','run',message='生成展示报告')
        dispatch.assert_awaited_once_with('model','run','display_report','bound')
