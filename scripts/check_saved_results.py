"""Rebuild evidence in a temporary copy and compare with a baseline.

No simulation or model API call is made. The supplied run is read-only.
Use --baseline-code to compare with the current working tree before refactoring.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            value.update(block)
    return value.hexdigest()


def relocate(value, old, new):
    if isinstance(value,dict):
        return {relocate(k,old,new):relocate(v,old,new) for k,v in value.items()}
    if isinstance(value,list):
        return [relocate(v,old,new) for v in value]
    if isinstance(value,str):
        return value.replace(str(old),str(new)).replace(old.as_posix(),new.as_posix())
    return value


def rebuild(code, root, module):
    worker='''import importlib,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
module=importlib.import_module(sys.argv[3])
root=Path(sys.argv[2])
with patch.object(module,'resolve_run_root',return_value=root):
    result=module.build_evidence_for_run(root.parents[1].name,root.name)
print(json.dumps({'evidence_count':result['evidence_count']}))
'''
    subprocess.run([sys.executable,'-c',worker,str(code),str(root),module],
                   cwd=root,check=True,capture_output=True,text=True,encoding='utf-8')
    return json.loads((root/'evidence/evidence_package.json').read_text(encoding='utf-8'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True,help='Existing models/<model>/runs/<run> directory')
    parser.add_argument('--baseline-code',type=Path,help='Optional Agentic2 code root; never modified')
    parser.add_argument('--temp-root',type=Path,help='Writable directory for disposable copies')
    args=parser.parse_args()
    source=args.run.resolve()
    original_model=source.parents[1]
    code=Path(__file__).resolve().parents[1]
    tracked={p.relative_to(source).as_posix():digest(p) for p in source.rglob('*') if p.is_file()}
    baseline=json.loads((source/'evidence/evidence_package.json').read_text(encoding='utf-8'))
    if args.temp_root:
        args.temp_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='saved-results-',dir=args.temp_root) as temporary:
        model=Path(temporary).resolve()/original_model.name
        root=model/'runs'/source.name
        shutil.copytree(source,root,ignore=shutil.ignore_patterns('diagnosis_tasks','__pycache__'))
        for name in ('static','mapping'):
            if (original_model/name).exists():shutil.copytree(original_model/name,model/name)
        for name in ('model.yaml','simulation_timing.json'):
            if (original_model/name).is_file():shutil.copy2(original_model/name,model/name)
        # Rebind recorded paths to the copy; never follow the old run for new writes.
        for p in root.glob('*.json'):
            value=json.loads(p.read_text(encoding='utf-8'))
            p.write_text(json.dumps(relocate(value,original_model,model),ensure_ascii=False,indent=2),encoding='utf-8')
        expected=relocate(baseline,original_model,model)
        if args.baseline_code:
            expected=rebuild(args.baseline_code.resolve(),root,'workflow_agents.evidence_builder')
        actual=rebuild(code,root,'workflow_agents.evidence.evidence_builder')
        # This field records extraction time, not rainfall/simulation time.
        for package in (expected,actual):
            for row in package['evidence_rows']:
                if row.get('metric_name')=='rainfall_context':
                    row['value'].pop('created_at',None)
        # Without a fresh code baseline, the summary digest changes when its paths are rebound.
        if not args.baseline_code:
            expected['source_hashes']['summary.json']=actual['source_hashes']['summary.json']
        mismatched=[key for key in sorted(expected.keys()|actual.keys()) if expected.get(key)!=actual.get(key)]
        differences=[]
        def compare(left, right, path=''):
            if len(differences)>=8 or left==right:
                return
            if isinstance(left,dict) and isinstance(right,dict):
                for key in sorted(left.keys()|right.keys()):
                    compare(left.get(key),right.get(key),path+'/'+key)
            elif isinstance(left,list) and isinstance(right,list) and len(left)==len(right):
                for i,(a,b) in enumerate(zip(left,right)):
                    compare(a,b,path+'/'+str(i))
            else:
                differences.append({'path':path,'before':str(left)[:300],'after':str(right)[:300]})
        compare(expected,actual)
        unchanged=tracked=={p.relative_to(source).as_posix():digest(p) for p in source.rglob('*') if p.is_file()}
        result={'status':'passed' if not mismatched and unchanged else 'failed',
                'run_id':source.name,'evidence_count':len(actual['evidence_rows']),
                'event_count':actual['overview'].get('event_count'),
                'source_run_unchanged':unchanged,'different_sections':mismatched,
                'first_differences':differences,
                'ignored_metadata':['rainfall_context.created_at (extraction timestamp)'],
                'baseline':'current_source_code' if args.baseline_code else 'saved_evidence_package',
                'scope':'saved-result evidence construction; no new simulation or LLM call'}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
