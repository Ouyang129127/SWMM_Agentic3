"""Agreed opening and verified sources for surface depth interpretation."""
import json
from pathlib import Path


BASIS = json.loads((Path(__file__).parent/'prompts'/'surface_basis_v1.json').read_text(encoding='utf-8'))
VERSION = BASIS['version']
