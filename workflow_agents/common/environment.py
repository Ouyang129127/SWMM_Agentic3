"""Read an optional local .env without overriding shell settings."""
import os
from pathlib import Path

def load_dotenv(path):
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
