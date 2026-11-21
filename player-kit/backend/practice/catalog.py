"""Local map files and built-in Bot templates."""
import json
import os
from pathlib import Path
import random

from game_core import mapgen

from .. import KIT_ROOT
from ..runtime.submission import submission_language


BOTS_DIR = KIT_ROOT / 'bots'
DEFAULT_BOT_ID = 'my_bot_python'
DEFAULT_BOT = f'bots/{DEFAULT_BOT_ID}'


def load_maps(path=None):
    """Read maps from a specified JSON file or the ARENA_MAPS catalog."""
    path = path or os.environ.get('ARENA_MAPS')
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as source:
        return {board['id']: board for board in json.load(source)}


def generate_maps(seed, count, output=None):
    if seed is None:
        seeds = (random.randrange(1, 10 ** 6) for _ in range(count))
    else:
        seeds = range(seed, seed + count)
    maps = [mapgen.generate(value) for value in seeds]
    if output is not None:
        Path(output).write_text(json.dumps(maps, ensure_ascii=False, indent=2), encoding='utf-8')
    return maps


def list_bots():
    if not BOTS_DIR.is_dir():
        return []
    bots = []
    for directory in sorted(BOTS_DIR.iterdir(), key=lambda path: (path.name != DEFAULT_BOT_ID, path.name)):
        try:
            submission_language(directory)
            readme = (directory / 'README.md').read_text(encoding='utf-8')
        except (ValueError, OSError):
            continue
        paragraphs = (text.strip() for text in readme.split('\n\n'))
        desc = next((text for text in paragraphs if text and not text.startswith(('#', '>', '|', '```'))
                     and '在这里' not in text), '')
        name = directory.name
        if name == DEFAULT_BOT_ID and not desc:
            desc = f'修改 {DEFAULT_BOT}/strategy.py，并在 README.md 中说明策略'
        desc = desc.replace('\n', '').replace('`', '').replace('**', '')
        bots.append({'id': name, 'name': name, 'desc': desc[:160], 'path': f'bots/{name}'})
    return bots
