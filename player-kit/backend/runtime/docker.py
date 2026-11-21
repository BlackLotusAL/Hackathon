"""Build and run C++ and Java submissions with the kit's Docker recipes."""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from .. import KIT_ROOT
from .submission import bot_dir, submission_language


PLATFORM = 'linux/amd64'
MIRROR_ENV = {'cpp': 'ARENA_CMAKE_INDEX_URL', 'java': 'ARENA_MAVEN_MIRROR_URL'}
RUNNERS = Path(__file__).with_name('runners')


def build_image(directory, tag, *, log=None):
    resolved = bot_dir(directory)
    if resolved is None:
        raise ValueError(f'找不到提交目录 {directory}')
    root = Path(resolved)
    language = submission_language(root)
    if language not in MIRROR_ENV:
        raise ValueError('镜像构建使用 C++ 或 Java 提交目录')

    runner = RUNNERS / language
    command = ['docker', 'buildx', 'build', '--load', '--platform', PLATFORM, '-t', tag,
               '--build-context', f'runner={runner}']
    if language == 'cpp':
        command += [
            '--build-context', f"game_core={KIT_ROOT / 'game_core'}",
            '--build-context', f"bot_backend={KIT_ROOT / 'backend'}",
        ]
    if log is not None:
        command += ['--progress', 'plain']

    key = MIRROR_ENV[language]
    value = os.environ.get(key)
    if value:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.query or url.fragment or any(c.isspace() or c in '<>&' for c in value)):
            raise ValueError(f'{key} 须为 HTTP(S) 镜像地址')
        command += ['--build-arg', f'{key}={value}']

    with TemporaryDirectory(prefix='player-kit-build-') as temporary:
        recipe = Path(temporary) / 'Dockerfile'
        shutil.copyfile(runner / 'Dockerfile', recipe)
        if not os.path.lexists(root / '.dockerignore'):
            shutil.copyfile(RUNNERS / '.dockerignore', recipe.with_name('Dockerfile.dockerignore'))
        try:
            subprocess.run(command + ['-f', str(recipe), str(root)], check=True, timeout=600,
                           stdout=log, stderr=subprocess.STDOUT if log is not None else None)
        except FileNotFoundError as error:
            raise ValueError('请先安装并启动 Docker 与 Buildx') from error
        except subprocess.SubprocessError as error:
            log_path = f'，日志位于 {log.name}' if log is not None else ''
            raise ValueError(f'{language} 镜像构建失败{log_path}') from error


def _remove(*args):
    try:
        subprocess.run(['docker', *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def launch_command(directory, port, resources, *, log):
    """Build a private image and register cleanup before the Bot is started."""
    name = 'exposure-tools-' + secrets.token_hex(8)
    image = f'{name}:local'
    resources.callback(_remove, 'image', 'rm', image)
    build_image(directory, image, log=log)
    resources.callback(_remove, 'container', 'rm', '-f', name)
    return [
        'docker', 'run', '--rm', '--name', name, '--platform', PLATFORM,
        '--publish', f'127.0.0.1:{port}:8080',
        '--env', 'BOT_HOST=0.0.0.0', '--env', 'BOT_PORT=8080', '--env', 'BOT_TOKEN',
        '--cpus', '1', '--memory', '1g', '--memory-swap', '1g', '--pids-limit', '128',
        '--read-only', '--tmpfs', '/tmp:rw,size=128m,mode=1777',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', image,
    ]
