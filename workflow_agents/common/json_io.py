import json

def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def _write(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)
