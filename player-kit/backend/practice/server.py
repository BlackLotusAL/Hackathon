"""HTTP and live event endpoints for the local practice interface."""
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from game_core.protocol import dumps

from .. import KIT_ROOT
from .catalog import list_bots
from .service import Practice


PAGE = KIT_ROOT / 'frontend' / 'index.html'
MATCH_RESOURCE = re.compile(r'/api/matches/([\w.-]+)(?:/(events|replay|turns/(\d+)|stderr/([01])))?')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'arena-local'

    @property
    def practice(self):
        return self.server.practice

    def log_message(self, fmt, *args):
        if os.environ.get('ARENA_SERVER_LOG'):
            sys.stderr.write('[server] ' + fmt % args + '\n')

    def send(self, code, obj=None, *, ctype='application/json; charset=utf-8', raw=None, extra=None):
        body = raw if raw is not None else (dumps(obj).encode('utf-8') if obj is not None else b'')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def body(self):
        size = int(self.headers.get('Content-Length') or 0)
        if not 0 <= size <= 64 * 1024 * 1024:
            raise ValueError('请求体大小无效')
        return self.rfile.read(size) if size else b''

    def json_body(self):
        raw = self.body()
        data = json.loads(raw.decode('utf-8')) if raw else {}
        if not isinstance(data, dict):
            raise ValueError('请求体须为 JSON 对象')
        return data

    def do_GET(self):
        self.dispatch(self.route_get)

    def do_POST(self):
        self.dispatch(self.route_post)

    def dispatch(self, route):
        try:
            route(self.path.split('?', 1)[0])
        except KeyError as error:
            self.send(404, {'error': f'找不到：{error}'})
        except (ValueError, TypeError, OSError, RecursionError) as error:
            self.send(400, {'error': str(error)})

    def route_get(self, path):
        practice = self.practice
        if path in ('/', '/index.html'):
            return self.send(200, raw=PAGE.read_bytes(), ctype='text/html; charset=utf-8')
        if path == '/api/maps':
            return self.send(200, practice.map_list())
        match = re.fullmatch(r'/api/maps/([\w-]+)', path)
        if match:
            return self.send(200, practice.get_map(match[1]))
        if path == '/api/bots':
            return self.send(200, list_bots())
        if path == '/api/matches':
            return self.send(200, practice.match_list())
        match = MATCH_RESOURCE.fullmatch(path)
        if match:
            return self.match_resource(*match.groups())
        if path == '/api/replays':
            return self.send(200, practice.replays.list_replays())
        match = re.fullmatch(r'/api/replays/([\w.-]+)', path)
        if match:
            return self.send(200, raw=practice.replays.replay_path(match[1]).read_bytes())
        if path == '/api/tournaments':
            return self.send(200, practice.tournament_list())
        match = re.fullmatch(r'/api/tournaments/([\w.-]+)', path)
        if match:
            return self.send(200, practice.get_tournament(match[1]).status())
        raise KeyError(path)

    def match_resource(self, match_id, resource, turn, seat):
        practice = self.practice
        if resource == 'replay':
            data = practice.replays.replay_path(match_id).read_bytes()
            return self.send(200, raw=data, extra={'Content-Disposition': f'attachment; filename="{match_id}.json"'})
        if seat is not None:
            log = practice.replays.stderr_path(match_id, seat)
            return self.send(200, raw=log.read_bytes()[-200000:] if log.is_file() else b'',
                             ctype='text/plain; charset=utf-8')
        live = practice.get_match(match_id)
        if resource == 'events':
            return self.stream(live)
        if turn is not None:
            return self.send(200, live.debug_turn(int(turn)))
        return self.send(200, live.status())

    def route_post(self, path):
        practice = self.practice
        if path == '/api/matches':
            live = practice.start_match(self.json_body())
            return self.send(201, {'id': live.id})
        if path == '/api/check':
            config = self.json_body()
            items, name = practice.check_bot(config.get('target'), quick=bool(config.get('quick')))
            return self.send(200, {'name': name, 'items': [
                {'name': label, 'level': level, 'detail': detail} for label, level, detail in items
            ]})
        if path == '/api/replays':
            return self.send(201, {'id': practice.replays.import_replay(self.body())})
        if path == '/api/tournaments':
            tournament = practice.start_tournament(self.json_body())
            return self.send(201, {'id': tournament.id})
        match = re.fullmatch(r'/api/tournaments/([\w.-]+)/stop', path)
        if match:
            tournament = practice.get_tournament(match[1])
            tournament.stop()
            return self.send(200, tournament.status())
        match = re.fullmatch(r'/api/matches/([\w.-]+)/(control|human)', path)
        if match:
            live = practice.get_match(match[1])
            config = self.json_body()
            if match[2] == 'control':
                return self.send(200, live.control(config.get('action'), config.get('ms')))
            default_seat = min(live.humans, default=0)
            live.press(int(config.get('seat', default_seat)), config.get('cmd'))
            return self.send(204)
        raise KeyError(path)

    def stream(self, live):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        offset, synced = 0, False
        try:
            while True:
                batch = live.events_after(offset, timeout=15 if synced else 0)
                offset += len(batch)
                if not batch and synced:
                    self.wfile.write(b': keepalive\n\n')
                for event in batch:
                    self.wfile.write(b'data: ' + dumps(event).encode('utf-8') + b'\n\n')
                finished = batch and batch[-1]['ev'] == 'end'
                if not synced and not finished:
                    self.wfile.write(b'data: {"ev":"synced"}\n\n')
                self.wfile.flush()
                if finished:
                    return
                synced = True
        except (BrokenPipeError, ConnectionResetError):
            pass


class PracticeServer(ThreadingHTTPServer):
    def __init__(self, address, practice):
        self.practice = practice
        super().__init__(address, Handler)


def serve(*, host='127.0.0.1', port=8080, open_browser=False, data_dir=None):
    practice = Practice(data_dir)
    practice.replays.root.mkdir(parents=True, exist_ok=True)
    with PracticeServer((host, port), practice) as server:
        url_host = '127.0.0.1' if host in ('0.0.0.0', '') else host
        url = f'http://{url_host}:{server.server_port}'
        print(f'曝光领地本地测试服务已启动：{url}   （Ctrl+C 退出；对局保存在 {practice.replays.root}）', flush=True)
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            practice.close()
