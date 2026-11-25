"""Build, start, and clean up Bot processes for local matches."""
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from tempfile import TemporaryDirectory

from sdk.cpp.build import build_bot as build_cpp
from sdk.java.build import build_bot as build_java, java_executable
from sdk.submission import KIT_ROOT, submission_language

from .transport import HttpBot


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ProcessBot(HttpBot):
    """Launch a Bot and start its readiness check after launch."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.language = submission_language(self.directory)
        self.proc = None
        self.resources = ExitStack()

    def start(self, limit_s):
        if self.proc is None:
            self._launch()
        return super().start(limit_s)

    def _launch(self):
        try:
            args, cwd = self._command()
            port = _free_port()
            super().__init__(port)
            env = dict(os.environ, BOT_HOST='127.0.0.1', BOT_PORT=str(port))
            options = {'start_new_session': True} if os.name != 'nt' else {}
            self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=sys.stderr, **options)
        except BaseException:
            self.close()
            raise

    def _command(self):
        python = [sys.executable, '-E', '-B', '-u', '-m']
        if self.language == 'python':
            return python + ['sdk.python', str(self.directory)], KIT_ROOT
        workspace = Path(self.resources.enter_context(TemporaryDirectory(prefix='player-kit-build-')))
        if self.language == 'cpp':
            binary = build_cpp(self.directory, workspace)
            return python + ['sdk.cpp.cpp_gateway', str(binary), '--cwd', str(self.directory)], KIT_ROOT
        java = java_executable()
        jar = build_java(self.directory, workspace)
        return [java, '-cp', str(jar), 'bot.Main'], self.directory

    def turn(self, run_id, msg, limit_s):
        deadline = time.monotonic() + limit_s
        reply = super().turn(run_id, msg, limit_s)
        if reply[1] == 'connection':
            # The socket can close before Windows signals process termination.
            try:
                self.proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        return reply

    def exited(self):
        return self.proc is not None and self.proc.poll() is not None

    def _terminate(self):
        if os.name != 'nt':
            os.killpg(self.proc.pid, signal.SIGTERM)
            return
        try:
            subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not self.exited():
            self.proc.terminate()

    def close(self):
        try:
            if self.proc is not None and not self.exited():
                try:
                    self._terminate()
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
