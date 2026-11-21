"""HTTP service and strategy dispatch for the Bot SDK.

Python strategies receive InitState and TurnState through StrategyRunner.
"""
import os
import re
import sys
import threading
import traceback

from game_core.protocol import MAX_REQUEST_BYTES, VERSION, dumps, json_object

from .state import InitState, TurnState

SDK_VERSION = 'python/2'


class StrategyRunner:
    """Construct a strategy from typed init data and pass it typed turns."""

    def __init__(self, factory):
        self.factory = factory
        self.init_message = None
        self.init = None
        self.strategy = None

    def handle(self, msg):
        kind = msg['type']
        if kind == 'init':
            self.init_message = msg
            self.init = InitState.from_message(msg)
            self.strategy = self.factory(self.init)
        elif kind == 'turn':
            state = TurnState.from_message(self.init, self.init_message, msg)
            return {'turn': state.turn, 'cmd': self.strategy.act(state)}
        elif hasattr(self.strategy, 'end'):
            self.strategy.end(msg)
        return None


PATH = re.compile(r'/v1/matches/([A-Za-z0-9_.-]{1,64})/(init|end|turns/([1-9][0-9]{0,2}))')


class _Match:
    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.Lock()
        self.newest = 0


def make_server(*, runner_factory, port=None, host=None, token=None, name=None, quiet=True):
    """Wrap a strategy factory or a raw-message runner factory in the v1 HTTP service."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if name is None:
        main_file = getattr(sys.modules.get('__main__'), '__file__', '')
        name = os.path.basename(os.path.dirname(os.path.abspath(main_file))) or 'bot'
    token = token if token is not None else os.environ.get('BOT_TOKEN', '')
    matches = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        disable_nagle_algorithm = True

        def log_message(self, fmt, *args):
            if not quiet:
                super().log_message(fmt, *args)

        def reply(self, code, obj=None):
            body = dumps(obj).encode('utf-8') if obj is not None else b''
            head = 'HTTP/1.1 %d %s\r\n' % (code, self.responses[code][0])
            head += 'Connection: close\r\n'
            self.close_connection = True
            if code != 204:
                head += 'Content-Length: %d\r\n' % len(body)
            if body:
                head += 'Content-Type: application/json\r\n'
            try:
                self.wfile.write(head.encode('ascii') + b'\r\n' + body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The referee closed a timed-out request.

        def authorized(self):
            if token and self.headers.get('X-Arena-Token') != token:
                self.reply(401)
                return False
            if self.headers.get('X-Arena-Protocol') != VERSION:
                self.reply(400)
                return False
            return True

        def do_GET(self):
            if self.authorized():
                if self.path == '/v1/health':
                    self.reply(200, {'ready': True, 'name': name, 'sdk': SDK_VERSION})
                else:
                    self.reply(404)

        def do_POST(self):
            if not self.authorized():
                return
            match = PATH.fullmatch(self.path)
            if match is None:
                return self.reply(404)
            match_id, endpoint, number = match.groups()
            kind = 'turn' if number else endpoint
            turn = int(number) if number else None
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_REQUEST_BYTES:
                    return self.reply(413)
                msg = json_object(self.rfile.read(size))
                if msg.get('type') != kind:
                    return self.reply(400)
                if turn is not None and (type(msg.get('turn')) is not int or msg['turn'] != turn or turn > 600):
                    return self.reply(400)
            except (ValueError, RecursionError):
                return self.reply(400)
            try:
                status, output = self.dispatch(match_id, kind, turn, msg)
            except Exception:
                traceback.print_exc()
                return self.reply(500)
            self.reply(status, output)

        def dispatch(self, match_id, kind, turn, msg):
            if kind == 'init':
                with lock:
                    if match_id in matches:
                        return 409, None
                    match = _Match(runner_factory())
                    match.lock.acquire()
                    matches[match_id] = match
                try:
                    match.runner.handle(msg)
                finally:
                    match.lock.release()
                return 204, None

            with lock:
                match = matches.get(match_id)
                if match is None:
                    return 404, None
                if kind == 'end':
                    del matches[match_id]
                else:
                    if turn <= match.newest:
                        return 409, None
                    match.newest = turn
            with match.lock:
                if kind == 'end':
                    match.runner.handle(msg)
                    return 204, None
                with lock:
                    if matches.get(match_id) is not match or turn < match.newest:
                        return 409, None
                output = match.runner.handle(msg)
                if (not isinstance(output, dict) or not isinstance(output.get('cmd'), str)
                        or not re.fullmatch(r'[UDLR]\+?', output['cmd'])):
                    return 500, None
                return 200, output

    port = int(port if port is not None else os.environ.get('BOT_PORT', 8080))
    host = host if host is not None else os.environ.get('BOT_HOST', '0.0.0.0')
    return ThreadingHTTPServer((host, port), Handler)


def run(*, runner_factory, port=None, name=None):
    """Start the HTTP service until terminated."""
    if port is None and '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    with make_server(port=port, runner_factory=runner_factory, name=name) as server:
        print('[sdk] listening on %s:%d' % server.server_address[:2], file=sys.stderr, flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
