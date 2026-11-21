"""Read the language manifest and validate the required submission files."""
import json
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED = {
    'python': ('strategy.py', 'requirements.txt'),
    'cpp': ('strategy.hpp', 'CMakeLists.txt'),
    'java': ('pom.xml', 'Strategy.java'),
}


def submission_language(root):
    root = Path(root)
    try:
        manifest = json.loads((root / 'bot.json').read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise ValueError(f'{root} 缺少 bot.json') from error
    except (ValueError, RecursionError) as error:
        raise ValueError('bot.json 必须是 UTF-8 编码的 JSON 对象') from error

    language = manifest.get('language') if isinstance(manifest, dict) else None
    if not isinstance(language, str) or language not in REQUIRED:
        raise ValueError('bot.json 的 language 须为 python、cpp 或 java')
    missing = [name for name in ('README.md', *REQUIRED[language]) if not (root / name).is_file()]
    if missing:
        raise ValueError(f'{root} 缺少 ' + '、'.join(missing))
    try:
        if not (root / 'README.md').read_text(encoding='utf-8').strip():
            raise ValueError('请在 README.md 中说明策略和依赖')
        if language == 'python':
            (root / 'requirements.txt').read_text(encoding='utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('README.md 和 requirements.txt 须使用 UTF-8 编码') from error
    return language
