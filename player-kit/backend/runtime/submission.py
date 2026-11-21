"""Read the language manifest and validate the required submission files."""
import json
import os
from pathlib import Path

from .. import KIT_ROOT


REQUIRED = {
    'python': ('strategy.py', 'requirements.txt'),
    'cpp': ('strategy.hpp', 'CMakeLists.txt'),
    'java': ('pom.xml', 'src/main/java/arena/bot/Strategy.java'),
}


def bot_dir(spec, cwd=None):
    name = str(spec).removeprefix('inproc:')
    for base in (cwd or os.getcwd(), KIT_ROOT):
        path = Path(base) / name
        if path.is_dir():
            return str(path.resolve())
    return None


def submission_language(root):
    root = Path(root)
    try:
        manifest = json.loads((root / 'bot.json').read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise ValueError('提交根目录缺少 bot.json') from error
    except (ValueError, RecursionError) as error:
        raise ValueError('bot.json 必须是 UTF-8 编码的 JSON 对象') from error

    language = manifest.get('language') if isinstance(manifest, dict) else None
    if not isinstance(language, str) or language not in REQUIRED:
        raise ValueError('bot.json 的 language 须为 python、cpp 或 java')
    missing = [name for name in ('README.md', *REQUIRED[language]) if not (root / name).is_file()]
    if missing:
        raise ValueError('提交根目录缺少 ' + '、'.join(missing))
    try:
        if not (root / 'README.md').read_text(encoding='utf-8').strip():
            raise ValueError('请在 README.md 中说明策略和依赖')
        if language == 'python':
            python_requirements(root)
    except UnicodeDecodeError as error:
        raise ValueError('README.md 和 requirements.txt 须使用 UTF-8 编码') from error
    return language


def python_requirements(root):
    lines = (Path(root) / 'requirements.txt').read_text(encoding='utf-8').splitlines()
    return [line for line in (line.strip() for line in lines) if line and not line.startswith('#')]
