"""A reusable Report Agent entry point; failed generations never replace HTML."""
import asyncio
import base64
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image

from workflow_agents.common.hashing import sha256_file
from workflow_agents.reporting.materials import SECTIONS, assert_sources_current, prepare_materials, verify_task
from workflow_agents.reporting.narrative import write_narrative
from workflow_agents.reporting.plots import make_plots
from workflow_agents.reporting.render import render

VERSION = 'report_agent_v1.10_surface_basis_palette'


def producer_hashes():
    paths=list(Path(__file__).parent.glob('*.py'))+list((Path(__file__).parent/'prompts').glob('*'))+[Path(__file__).parents[1]/'common'/'llm.py']
    return {str(path.resolve()):sha256_file(path) for path in paths}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


class DocumentCheck(HTMLParser):
    def __init__(self):
        super().__init__(); self.sections=[]; self.events=[]; self.images=0; self.text=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='section':self.sections.append(attrs.get('id'))
        if tag=='article':self.events.append(attrs.get('data-node'))
        if tag=='img':
            src=attrs.get('src','')
            if not src.startswith('data:image/'):raise ValueError('Report image is not embedded')
            with Image.open(BytesIO(base64.b64decode(src.split(',',1)[1],validate=True))) as image:
                image.verify()
            self.images+=1

    def handle_data(self, data):self.text.append(data)


def check_html(path, materials):
    checker=DocumentCheck();checker.feed(path.read_text(encoding='utf-8'))
    if checker.sections!=list(SECTIONS):raise ValueError('HTML section coverage changed')
    if checker.events!=[e['node_id'] for e in materials['events']]:raise ValueError('HTML event coverage changed')
    if any('[[' in s or ']]' in s for s in checker.text):raise ValueError('HTML has unresolved facts')
    if checker.images<4:raise ValueError('HTML lacks required charts')
    return dict(status='passed',sections=len(checker.sections),events=len(checker.events),
                embedded_images=checker.images,all_images_verified=True,
                numeric_policy='supplied_fact_tokens',causal_policy='current_diagnosis_statuses',
                verification_scope='structure, source identity and references; not causal certification')


async def generate_display_report(root, directory, state, complete=None):
    root,directory=Path(root),Path(directory)
    # Mutable task.json is intentionally outside source_hashes. The exact
    # diagnosis revision and immutable evidence binding are recorded separately.
    previous=state.get('display_report',{})
    producer=producer_hashes()
    if previous.get('status')=='completed' and previous.get('revision')==state['revision'] and previous.get('version')==VERSION and previous.get('producer_hashes')==producer:
        try:
            verify_task(root,directory,state)
            old=json.loads(Path(previous['materials_file']).read_text(encoding='utf-8'))
            assert_sources_current(old)
            if sha256_file(Path(previous['html_file']))==previous['html_sha256']:
                return dict(previous, reused=True)
        except (OSError,ValueError,KeyError):pass
    generated_at=datetime.now().astimezone().isoformat(timespec='seconds')
    output=directory/'display_reports'/f"r{state['revision']}"/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    result=dict(status='running',version=VERSION,task_id=state['task_id'],revision=state['revision'],
                generated_at=generated_at,output_dir=str(output.resolve()),producer_hashes=producer)
    started=time.monotonic();phase='materials'
    try:
        materials,data=await asyncio.to_thread(prepare_materials,root,directory,state)
        save(output/'report_materials.json',materials)
        phase='plots'
        media,layouts=await asyncio.to_thread(make_plots,materials,data,output/'figures')
        save(output/'map_label_layout.json',layouts)
        assert_sources_current(materials)
        phase='narrative'
        narrative,facts=await write_narrative(materials,data['known_ids'],output,complete)
        save(output/'narrative.json',narrative);save(output/'fact_text.json',facts)
        phase='render'
        # Reject a revision or source mutation occurring during an API call.
        current=json.loads((directory/'task.json').read_text(encoding='utf-8'))
        if current['revision']!=state['revision'] or current.get('diagnosis_hash')!=state['diagnosis_hash']:
            raise ValueError('Diagnosis revision changed during report generation')
        verify_task(root,directory,state);assert_sources_current(materials)
        if producer_hashes()!=producer:raise ValueError('Report implementation changed during generation')
        path=output/'模拟诊断报告.html'
        render(materials,narrative,facts,media,path,generated_at)
        phase='quality_checks'
        checks=await asyncio.to_thread(check_html,path,materials)
        save(output/'quality_checks.json',checks)
        result.update(status='completed',html_file=str(path.resolve()),html_sha256=sha256_file(path),
                      materials_file=str((output/'report_materials.json').resolve()),quality_checks=checks)
    except Exception as exc:
        result.update(status='failed',phase=phase,error_type=type(exc).__name__,error=str(exc))
    result['elapsed_seconds']=round(time.monotonic()-started,3)
    save(output/'generation.json',result)
    return result


async def attach_display_report(root, directory, state, complete=None):
    result=await generate_display_report(root,directory,state,complete)
    old=state.get('display_report')
    if old and old.get('status')=='completed' and result['status']!='completed':
        state['previous_display_report']=old
    state['display_report']=result
    if result['status']=='completed':
        state.update(display_report_file=result['html_file'],display_report_revision=state['revision'])
    else:
        state.pop('display_report_file',None);state.pop('display_report_revision',None)
    return result
