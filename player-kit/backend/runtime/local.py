"""Resolve and run Bot submissions, commands and remote services."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from game_core.protocol import dumps, json_object, parse_reply

from .. import KIT_ROOT
from .client import HttpBot
from .docker import launch_command
from .sdk import StrategyRunner
from .submission import bot_dir, submission_language


IMPORT_LOCK = threading.Lock()


def command_args(spec):
    args = shlex.split(spec, posix=os.name != 'nt')
    if os.name == 'nt':
        args = [arg[1:-1] if arg.startswith(('"', "'")) and arg.endswith(arg[0]) else arg for arg in args]
    if not args:
        raise ValueError('Bot 启动命令为空')
    if args[0] in ('python', 'python3'):
        args[0] = sys.executable
    return args


def bot_name(spec, cwd=None):
    if spec.startswith(('http://', 'https://')):
        return spec.split('://', 1)[1].rstrip('/')
    directory = bot_dir(spec, cwd)
    if directory:
        return Path(directory).name
    return Path(command_args(spec)[-1]).stem


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def fast_spec(spec):
    """Run Python submissions in-process when fast practice is enabled."""
    if not spec.startswith('inproc:'):
        directory = bot_dir(spec)
        if directory and submission_language(directory) == 'python':
            return 'inproc:' + spec
    return spec


class ManagedBot(HttpBot):
    """Connect to a Bot or manage its local process or container."""

    def __init__(self, spec, stderr_path=None, cwd=None):
        self.proc = None
        self.resources = ExitStack()
        if spec.startswith(('http://', 'https://')):
            super().__init__(spec)
            return

        directory = bot_dir(spec, cwd)
        port = free_port()
        token = secrets.token_hex(8)
        super().__init__(f'http://127.0.0.1:{port}', token=token)
        previous_path = os.environ.get('PYTHONPATH')
        env = dict(os.environ, BOT_HOST='127.0.0.1', BOT_PORT=str(port), BOT_TOKEN=token,
                   PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=str(KIT_ROOT) + (os.pathsep + previous_path if previous_path else ''))
        options = {'start_new_session': True} if os.name != 'nt' else {}
        try:
            log = self.resources.enter_context(open(stderr_path, 'wb')) if stderr_path else None
            if directory is None:
                args = command_args(spec)
            elif submission_language(directory) == 'python':
                entry = Path(__file__).with_name('strategy_entry.py')
                args = [sys.executable, '-I', '-B', '-u', str(entry), directory]
            else:
                args = launch_command(directory, port, self.resources, log=log)
            output = log if log is not None else subprocess.DEVNULL
            self.proc = subprocess.Popen(args, cwd=directory or cwd or KIT_ROOT, env=env,
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=output, **options)
        except BaseException:
            self.close()
            raise

    def exited(self):
        return self.proc is not None and self.proc.poll() is not None

    @property
    def exit_info(self):
        if self.exited():
            return {'exit_code': self.proc.returncode, 'oom': False}
        return None

    def close(self):
        try:
            if self.proc is not None and not self.exited():
                try:
                    if os.name == 'nt':
                        self.proc.terminate()
                    else:
                        os.killpg(self.proc.pid, signal.SIGTERM)
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == 'nt':
                            self.proc.kill()
                        else:
                            os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait()
                except ProcessLookupError:
                    self.proc.wait()
        finally:
            self.resources.close()


class InprocBot:
    """Run a trusted Python strategy in the same process for local practice."""

    kind = 'inproc'

    def __init__(self, spec, cwd=None):
        directory = bot_dir(spec, cwd)
        if directory is None:
            raise ValueError(f'找不到 Bot 目录 {spec.removeprefix("inproc:")}')
        if submission_language(directory) != 'python':
            raise ValueError('进程内模式使用 Python 策略')

        # Bundled strategies can have identically named helpers. Scope imports to this directory.
        with IMPORT_LOCK:
            before = set(sys.modules)
            sys.path.insert(0, directory)
            try:
                path = Path(directory) / 'strategy.py'
                module_spec = importlib.util.spec_from_file_location(f'_bot_{secrets.token_hex(6)}', path)
                module = importlib.util.module_from_spec(module_spec)
                sys.modules[module_spec.name] = module
                module_spec.loader.exec_module(module)
            finally:
                sys.path.remove(directory)
                for name in set(sys.modules) - before:
                    filename = getattr(sys.modules.get(name), '__file__', None)
                    if filename and os.path.abspath(filename).startswith(directory + os.sep):
                        del sys.modules[name]

        self.runner = StrategyRunner(module.Strategy)
        self.info = {'ready': True, 'name': Path(directory).name, 'sdk': 'inproc'}

    def exited(self):
        return False

    def start(self, limit_s):
        return True, 'ready'

    def _request(self, msg, limit):
        t0 = time.perf_counter()
        try:
            response = self.runner.handle(json_object(dumps(msg).encode('utf-8')))
            raw = None if response is None else dumps(response)
            status = 'ok'
        except Exception as error:
            raw, status = repr(error), 'invalid'
        elapsed = int((time.perf_counter() - t0) * 1e6)
        if elapsed > limit * 1e6:
            status = 'timeout'
        return status, raw, elapsed

    def post(self, mid, what, msg, limit_s):
        status, raw, elapsed = self._request(msg, limit_s)
        return status, 204 if status == 'ok' else 0, raw, elapsed

    def turn(self, mid, msg, limit_s):
        status, raw, elapsed = self._request(msg, limit_s)
        if status != 'ok':
            return None, status, raw, elapsed
        result = parse_reply(raw)
        if result is None or result[0] != msg['turn']:
            return None, 'invalid', raw, elapsed
        return result[1], 'ok', raw, elapsed

    def close(self):
        pass


def make_bot(spec, *, stderr_path=None, cwd=None):
    if not isinstance(spec, str):
        return spec
    if spec.startswith('inproc:'):
        return InprocBot(spec, cwd)
    return ManagedBot(spec, stderr_path, cwd)
