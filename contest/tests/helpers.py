"""Shared transports, process lifecycles and evidence builders for Contest tests."""
from base64 import b64encode
from contextlib import ExitStack, closing, contextmanager, nullcontext
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile


from engine.mapgen import generate
from engine.protocol import dumps, json_object
from judge.task import run_task
from service.duels import insert_plan
from service.server import handler_for
from service.workbook import MIME, ROSTER_HEADERS, SHEET_XML, write_workbook


CONTEST = Path(__file__).resolve().parents[1]
TEAMS = ['team001', 'team002']


def plan_duel(contest, duel_id, kind, sides=TEAMS):
    """Seed a known plan for tests of scheduling, archives or historical results."""
    rules = kind.split('-')[0]
    maps = [{'seed': 42}] if kind.endswith('-ladder') else contest.maps[rules]
    with contest.database.transaction() as connection:
        insert_plan(connection, kind, {'duel_id': duel_id, 'rules': rules, 'sides': sides}, maps)


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='contest-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


def run_module(package, *arguments, expected=0):
    environment = dict(os.environ, PYTHONIOENCODING='utf-8')
    completed = subprocess.run([sys.executable, '-B', '-m', package, *map(str, arguments)],
                               cwd=CONTEST, env=environment, capture_output=True,
                               encoding='utf-8', timeout=30)
    if completed.returncode != expected:
        raise AssertionError(f'{package} exited {completed.returncode}, expected {expected}: {completed.stderr}')
    return completed


def write_config(path, **overrides):
    source = CONTEST / 'config/contest.example.json'
    values = json.loads(source.read_text(encoding='utf-8'))
    if values.get('maps') is not None:
        values['maps'] = str(source.parent / values['maps'])
    values.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(values), encoding='utf-8')
    return path


@contextmanager
def running_service(config, *arguments):
    """Own the service process until the context exits, including startup failures."""
    log = config.with_suffix('.log')
    with log.open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(
            [sys.executable, '-B', '-m', 'service', '--config', str(config), 'serve', *arguments],
            cwd=CONTEST, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8')
    try:
        ready = Queue()
        Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        try:
            line = ready.get(timeout=15).strip()
        except Empty as error:
            raise AssertionError(log.read_text(encoding='utf-8')) from error
        address = re.fullmatch(r'Contest (http://(?:[0-9.]+|\[[0-9a-fA-F:]+\]):[0-9]+)(?:/admin/prelim)?', line)
        if address is None:
            raise AssertionError(log.read_text(encoding='utf-8'))
        yield address[1]
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def case(identifier, description):
    """Attach a stable priority and case ID without wrapping the test method."""
    if not re.fullmatch(r'L[012]-[0-9]{3}', identifier):
        raise ValueError(f'Invalid test case ID: {identifier}')

    def annotate(method):
        method.case_id = identifier
        method.__doc__ = f'{identifier}: {description}'
        return method

    return annotate


def sealed_maps(stage):
    """Supply three immutable map payloads with their published hashes."""
    maps = []
    for seed in (42, 43, 44):
        raw = dumps(generate(seed, stage)) + '\n'
        maps.append({'file': raw, 'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()})
    return maps


def write_map_pools(path, *stages):
    directory = path.parent / 'maps'
    directory.mkdir(parents=True, exist_ok=True)
    pools = {}
    for stage in stages:
        entries = []
        for number, spec in enumerate(sealed_maps(stage), 1):
            filename = f'{stage}-{number}.json'
            (directory / filename).write_bytes(spec['file'].encode('utf-8'))
            entries.append({'file': f'maps/{filename}', 'sha256': spec['sha256']})
        pools[stage] = entries
    path.write_text(dumps(pools), encoding='utf-8')
    return path


class _JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def read_json(self):
        return json_object(self.rfile.read(int(self.headers['Content-Length'])))

    def reply(self, status, payload=None):
        body = dumps(payload).encode('utf-8') if payload is not None else b''
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        if payload is not None:
            self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _http_server(handler):
    server = HTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def http_bots(run_id):
    """Run one service per Bot and require the same execution ID on both."""
    class Handler(_JsonHandler):
        def valid_request(self):
            return self.path.startswith('/v1/') and self.headers.get('Authorization') is None

        def do_GET(self):
            if not self.valid_request():
                return self.reply(400)
            self.reply(200 if self.path == '/v1/health' else 404, {'ready': True})

        def do_POST(self):
            if not self.valid_request():
                return self.reply(400)
            seat = self.server.seat
            message = self.read_json()
            kind = message['type']
            action = f'turns/{message["turn"]}' if kind == 'turn' else kind
            if self.path != f'/v1/matches/{run_id}/{action}':
                return self.reply(400)
            if kind == 'init':
                if message['seat'] != seat or self.server.stage is not None:
                    return self.reply(400)
                self.server.stage = message['stage']
                return self.reply(204)
            if self.server.stage is None:
                return self.reply(404)
            if kind == 'end':
                self.server.stage = None
                return self.reply(204)
            final = self.server.stage == 'final'
            if any((field in message) != final for field in ('latent', 'latent_regions')):
                return self.reply(400)
            command = ('R', 'U', 'L', 'D')[((message['turn'] - 1) // 4) % 4] if seat == 0 else 'D'
            self.reply(200, {'turn': message['turn'], 'cmd': command})

    with ExitStack() as resources:
        servers = [resources.enter_context(_http_server(Handler)) for _ in TEAMS]
        for seat, server in enumerate(servers):
            server.seat, server.stage = seat, None
        yield [{'name': team, 'url': f'http://127.0.0.1:{server.server_port}'}
               for team, server in zip(TEAMS, servers)]
        if any(server.stage is not None for server in servers):
            raise AssertionError('Missing end request for one or more Bot services')


class Bot:
    exit_info = None

    def __init__(self, command, *, status='ok', us=1001, init='ok', exit_after_reply=False):
        self.command, self.status, self.us, self.init = command, status, us, init
        self.exit_after_reply = exit_after_reply

    def start(self, limit):
        return True, 'ready'

    def post(self, run_id, action, body, limit):
        return self.init if action == 'init' else 'ok', 204, None, 99

    def turn(self, run_id, body, limit):
        if self.exit_after_reply:
            self.exit_info = {'exit_code': 137, 'oom': True}
        us = self.us(body['turn']) if callable(self.us) else self.us
        return self.command, self.status, 'private-response', us

    def exited(self):
        return self.exit_info is not None


def run_query_payload(*runs, missing=()):
    return {'runs': list(runs), 'missing_run_ids': list(missing)}


@contextmanager
def http_arena(status):
    """Record requests to the Arena API."""
    requests = Queue()
    arena = ArenaStub()
    arena.summary = status

    class Handler(_JsonHandler):
        def record(self, body=None):
            requests.put({'method': self.command, 'path': self.path, 'body': body,
                          'headers': {key.lower(): value for key, value in self.headers.items()}})

        def do_POST(self):
            body = self.read_json()
            self.record(body)
            if self.path.endswith('/arena/v1/runs/query'):
                self.reply(*arena.query_runs(body['run_ids']))
            else:
                self.reply(arena.submit(body) if self.path.endswith('/arena/v1/runs') else 404)

        def do_GET(self):
            self.record()
            if self.path.endswith('/arena/v1/status'):
                self.reply(200, arena.status())
            else:
                self.reply(404)

    with _http_server(Handler) as server:
        yield f'http://127.0.0.1:{server.server_port}', requests, arena


def wait_for(read, condition, *, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if condition(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f'Timed out waiting for state: {value}')
        time.sleep(0.05)


class ArenaStub:
    def __init__(self):
        self.submitted = []
        self.queries = []
        self.states = {}
        self.summary = None

    def submit(self, request):
        self.submitted.append(request)
        run_id = request['run_id']
        self.states.setdefault(run_id, {'run_id': run_id, 'state': 'queued'})
        return 202

    def set_state(self, run_id, state, **fields):
        self.states[run_id] = {'run_id': run_id, 'state': state, **fields}

    def query_runs(self, run_ids):
        self.queries.append(list(run_ids))
        runs, missing = [], []
        for run_id in reversed(run_ids):
            run = self.states.get(run_id)
            if run is None:
                missing.append(run_id)
            else:
                runs.append(run)
        return 200, run_query_payload(*runs, missing=missing)

    def status(self):
        return self.summary


def admin_headers(user='organizer', password='contest-local'):
    credentials = b64encode(f'{user}:{password}'.encode('utf-8')).decode('ascii')
    return {'Authorization': 'Basic ' + credentials, 'X-Contest-Admin': '1'}


class _HandlerExchange:
    """The socket surface consumed by BaseHTTPRequestHandler in in-process tests."""
    def __init__(self, method, path, data, headers):
        lines = [f'{method} {path} HTTP/1.0', f'Content-Length: {len(data)}']
        lines.extend(f'{name}: {value}' for name, value in headers.items())
        self.source = BytesIO(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + data)
        self.response = BytesIO()

    def makefile(self, *args):
        return self.source

    def sendall(self, data):
        self.response.write(data)

    def reply(self):
        headers, _, body = self.response.getvalue().partition(b'\r\n\r\n')
        return int(headers.split()[1]), body


class HttpClient:
    """Use one request format for real TCP and in-process HTTP handlers."""
    def __init__(self, target, *, prefix='', headers=None):
        self.target = urlsplit(target) if isinstance(target, str) else target
        self.prefix = (self.target.path.rstrip('/') if isinstance(target, str) else '') + prefix.rstrip('/')
        self.headers = dict(headers or {})

    def request(self, path, body=None, *, method='GET', headers=None):
        path = self.prefix + '/' + path.lstrip('/')
        request_headers = {**self.headers, **(headers or {})}
        data = body
        if body is not None:
            if isinstance(body, bytes):
                request_headers.setdefault('Content-Type', MIME)
            else:
                data = dumps(body).encode('utf-8')
                request_headers.setdefault('Content-Type', 'application/json')
        if callable(self.target):
            exchange = _HandlerExchange(method, path, data or b'', request_headers)
            self.target(exchange, ('127.0.0.1', 0), None)
            return exchange.reply()
        with closing(HTTPConnection(self.target.hostname, self.target.port, timeout=5)) as connection:
            connection.request(method, path, body=data, headers=request_headers)
            with connection.getresponse() as response:
                return response.status, response.read()

    def post(self, path, body=None, *, headers=None):
        return self.request(path, body, method='POST', headers=headers)

    def json(self, path, body=None, *, method='GET', headers=None, status=200):
        code, payload = self.request(path, body, method=method, headers=headers)
        if code != status:
            raise AssertionError(f'{method} {path}: expected {status}, got {code}: {payload[:400]!r}')
        return json.loads(payload)


def contest_handler(contest, *, admin=None):
    class Handler(handler_for(contest, admin=admin)):
        def log_message(self, *args):
            pass
    return Handler


def write_judge_task(directory, payload, *, exited=False):
    directory.mkdir(parents=True, exist_ok=True)
    task = directory / 'task.json'
    task.write_text(dumps(payload), encoding='utf-8')
    if exited:
        exits = directory / 'exits'
        exits.mkdir()
        for seat in (0, 1):
            (exits / f'p{seat}.json').write_text(dumps({'exit_code': 137, 'oom': True}), encoding='utf-8')
    return task


def arena_archive(output, bots, *, state='finished', duel_started_at=None):
    """Write the manifest supplied with an Arena archive fixture."""
    manifest = {'bots': bots}
    if state == 'finished':
        result = json_object((output / 'result.json').read_bytes())
        fields = ('run_id', 'engine_version', 'map', 'map_sha256', 'winner', 'reason',
                  'score', 'bonus', 'lead_turns', 'time_us')
        manifest['result'] = {key: result[key] for key in fields}
    if duel_started_at is not None:
        manifest['duel_started_at'] = duel_started_at
    output.mkdir(parents=True, exist_ok=True)
    (output / 'match.json').write_text(dumps(manifest) + '\n', encoding='utf-8')
    return manifest


class MatchFixture(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.commits = {}

    def request(self, stage='prelim', run_id='run-1.1'):
        return {'run_id': run_id, 'duel_id': 'duel-1', 'rules': stage, 'bots': TEAMS,
                'config': {'map': {'seed': 42}}}

    def metadata(self, request):
        bots = []
        for team in request['bots']:
            entry = {'team_id': team}
            if team in self.commits:
                entry['commit'] = self.commits[team]
            elif team.startswith('t'):
                entry['commit'] = team[-3:] * 13 + '0'
            bots.append(entry)
        return bots

    def judge_archive(self, request, bots=None, *, started_at=None, archive_root=None):
        root = archive_root or self.root
        output = root / 'runs' / request['run_id']
        specs = [{'url': f'http://bot{seat}:8080', 'name': team}
                 for seat, team in enumerate(request['bots'])]
        task = write_judge_task(root / ('task-' + request['run_id']), dict(request, bots=specs), exited=bots is None)
        transport = nullcontext() if bots is None else patch('judge.task.ContainerBot', side_effect=bots)
        with transport:
            run_task(task, output, request['rules'])
        manifest = arena_archive(output, self.metadata(request), duel_started_at=started_at)
        replay = json_object((output / 'replay.json').read_bytes())
        return output, manifest, replay


def roster_workbook(rows, headers=ROSTER_HEADERS):
    return write_workbook((('队伍', headers, rows, len(rows)),))


def exported_tables(data):
    """Decode cell positions and types independently of the roster reader."""
    ns = f'{{{SHEET_XML}}}'
    with ZipFile(BytesIO(data)) as book:
        workbook = ET.fromstring(book.read('xl/workbook.xml'))
        tables = {}
        for sheet in workbook.find(ns + 'sheets'):
            index = sheet.attrib['sheetId']
            root = ET.fromstring(book.read(f'xl/worksheets/sheet{index}.xml'))
            all_rows = []
            width = len(root.find(ns + 'cols'))
            for row in root.find(ns + 'sheetData'):
                cells = [None] * width
                for cell in row:
                    letter = re.match('[A-Z]', cell.attrib['r'])[0]
                    value = cell.findtext(f'{ns}is/{ns}t') if cell.get('t') == 'inlineStr' else int(cell.findtext(ns + 'v'))
                    cells[ord(letter) - ord('A')] = value
                all_rows.append(cells)
            tables[sheet.attrib['name']] = all_rows
    return tables
