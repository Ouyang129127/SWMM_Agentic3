"""One project root; model and runtime locations may be configured explicitly."""
import os
from pathlib import Path
from .environment import load_dotenv

APP_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(APP_ROOT / ".env")
MODELS_DIR = Path(os.environ.get('SWMM_MODELS_DIR', str(APP_ROOT / 'models'))).expanduser().resolve()
RUNTIME_DIR = Path(os.environ.get('SWMM_RUNTIME_DIR', str(APP_ROOT / 'runtime'))).expanduser().resolve()
WEB_DIR = APP_ROOT / 'web'
