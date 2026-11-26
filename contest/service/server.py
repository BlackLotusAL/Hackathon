"""Contest HTTP endpoints, organizer access and service lifecycle."""
from base64 import b64encode
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
import hmac
import re
import socket
from threading import Event
from time import monotonic
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4

from engine.game import Game
from engine.maps import generate
from engine.match import HttpBot
from engine.protocol import RUN_ID, dumps, end_message, init_message, json_object, turn_message
from engine.rules import PARAMS, TIME_LIMIT_STATES

from .arena import ArenaClient, ContestWorker, ExecutionService
from .config import IPWhitelist
from .database import Database
from .domain import OFFICIAL_BATCH, PHASES, RULESET, ContestError
from .duels import DuelService, Rulings, load_pools
from .preliminary import Preliminary
from .workbook import MAX_UPLOAD, MIME


BOT_CHECK_TURNS = 60
DUEL_QUERY = re.compile(r'/contest/v1/duels/([^/]+)/(status|result)')
LOAD_TEST_RESOURCE = re.compile(r'/admin/load-tests/([^/]+)(?:/(retry|stop))?')
MANUAL_DUEL_RESOURCE = re.compile(r'/admin/duels/([^/]+)')
REPLAY_RESOURCE = re.compile(r'/admin/runs/([^/]+)/replay')


class Contest:
    """One shared database, with explicit feature services for every caller."""

    def __init__(self, path, pools=None, *, phase='prelim-ladder', shutdown=None):
        if phase not in PHASES:
            raise ValueError('配置 phase 须为 ' + '、'.join(PHASES))
        self.database = Database(path, shutdown=shutdown)
        self.maps = pools or {}
        self.duels = DuelService(self.database, phase)
        self.runs = ExecutionService(self.database)
        self.preliminary = Preliminary(self.database, self.maps, phase)
        self.rulings = Rulings(self.database)


def _local_bot(target):
    try:
        url = urlsplit(target)
        address = ip_address('127.0.0.1' if url.hostname == 'localhost' else url.hostname)
        if not address.is_loopback or url.username is not None or url.port == 0:
            raise ValueError()
        bot = HttpBot(target)
    except ValueError as error:
        raise ContestError(400, 'INVALID_CHECK_TARGET', '请填写本机 Bot 的 HTTP(S) 根地址') from error
    bot.host = str(address)
    return bot


def _http_detail(status, code):
    return f'HTTP {code}' if code else {'timeout': '响应超时', 'connection': '连接失败'}.get(status, '响应无效')


def _check_bot_turns(bot, run_id, game, record):
    errors = 0
    for turn in range(BOT_CHECK_TURNS):
        command, status, _, elapsed_us = bot.turn(run_id, turn_message(game), PARAMS['turn_ms'] / 1000)
        errors += status != 'ok'
        if status in TIME_LIMIT_STATES:
            elapsed_us = PARAMS['turn_ms'] * 1000
        game.time_us[0] += elapsed_us
        if turn == 0:
            record('回合响应', 'ok' if status == 'ok' else 'fail', command or _http_detail(status, 0))
        if errors and (turn == 0 or errors >= PARAMS['max_errors']):
            game.forfeit(0, 'errors')
            break
        game.step((command, None))

    mean_ms = game.time_us[0] / (turn + 1) / 1000
    level = 'fail' if errors else 'warn' if mean_ms > PARAMS['turn_ms'] * 0.7 else 'ok'
    record(f'试打 {BOT_CHECK_TURNS} 回合', level, f'{game.turn} 回合，错误 {errors} 次，平均 {mean_ms:.1f} 毫秒')


def check_bot(body):
    """Exercise the current Bot HTTP protocol with an isolated practice session."""
    rules, target = body.get('rules'), body.get('target')
    if body.keys() != {'rules', 'target'} or rules != RULESET:
        raise ContestError(400, 'INVALID_RULES', '请使用当前赛题规则（prelim）并填写 Bot 地址')
    if not isinstance(target, str) or not target.strip():
        raise ContestError(400, 'INVALID_CHECK_TARGET', '请填写 Bot 服务地址')
    target = target.strip()
    bot = _local_bot(target)
    items = []

    def record(name, level, detail):
        items.append({'name': name, 'level': level, 'detail': detail})

    report = {'name': target, 'rules': rules, 'items': items}
    started = monotonic()
    ready, _ = bot.start(PARAMS['startup_s'])
    if not ready:
        record('启动就绪', 'fail', 'Bot 未返回就绪状态')
        return report
    record('启动就绪', 'ok', f'{monotonic() - started:.2f} 秒就绪')

    run_id = f'run-{uuid4().int}.1'
    game = Game(generate(42))
    try:
        status, code, _, _ = bot.post(run_id, 'init', init_message(game, 0), PARAMS['init_ms'] / 1000)
        record('开局请求', 'ok' if status == 'ok' else 'fail', _http_detail(status, code))
        if status == 'ok':
            status, code, _, _ = bot.request(
                'POST', f'/v1/matches/run-{uuid4().int}.1/turns/1', turn_message(game), 1,
            )
            record('未知对局', 'ok' if status == 'ok' and code == 404 else 'warn', _http_detail(status, code))
            _check_bot_turns(bot, run_id, game, record)
    finally:
        if not game.done:
            game.forfeit(0, 'stopped')
        status, code, _, _ = bot.post(run_id, 'end', end_message(game), PARAMS['end_ms'] / 1000)
        record('结束通知', 'ok' if status == 'ok' else 'warn', _http_detail(status, code))

    return report


class Admin:
    def __init__(self, contest, user, password, arena=None, *, archive_root=None):
        self.preliminary, self.rulings = contest.preliminary, contest.rulings
        self.duels = contest.duels
        self.user, self.arena = user, arena
        self.archive_root = archive_root
        self.credentials = b64encode(f'{user}:{password}'.encode('utf-8'))
        self.page = Path(__file__).with_name('admin.html').read_bytes().replace(
            b'{{phase}}', escape(self.preliminary.phase).encode('utf-8'),
        )
        self.replay_script = Path(__file__).with_name('replay.js').read_bytes()

    @staticmethod
    def workbook(http, data, filename, fallback):
        http.respond(200, data, MIME, {
            'Content-Disposition': f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}',
        })

    def authorize(self, http, method):
        scheme, _, encoded = http.headers.get('Authorization', '').partition(' ')
        if scheme.lower() != 'basic' or not hmac.compare_digest(encoded.encode('utf-8'), self.credentials):
            raise ContestError(401, 'ADMIN_AUTH_REQUIRED', '请使用主办方管理账号登录')
        if method == 'POST' and http.headers.get('X-Contest-Admin') != '1':
            raise ContestError(403, 'ADMIN_HEADER_REQUIRED', '管理操作须经 Contest 后台提交')

    def handle(self, http, method):
        self.authorize(http, method)
        path, _, query = http.path.partition('?')
        if method == 'GET':
            self.get(http, path, query)
        else:
            self.post(http, path)

    def get(self, http, path, query):
        if path in ('/admin', '/admin/prelim', '/admin/prelim/'):
            return http.respond(200, self.page, 'text/html; charset=utf-8')
        if path == '/admin/replay.js':
            return http.respond(200, self.replay_script, 'text/javascript; charset=utf-8')
        if path == '/admin/replays':
            params = parse_qs(query)
            batch = params.get('batch', [OFFICIAL_BATCH])[0]
            page = params.get('page', ['0'])[0]
            if not re.fullmatch(r'[0-9]{1,6}', page):
                raise ContestError(400, 'INVALID_PAGE', '回放页码无效')
            return http.reply(200, self.duels.batch_replays(batch, int(page)))
        queries = {
            '/admin/prelim/progress': self.preliminary.progress,
            '/admin/prelim/standings': self.preliminary.standings,
            '/admin/load-tests': self.preliminary.load_tests,
            '/admin/duels': self.duels.manual_duels,
            '/admin/rulings': self.rulings.pending,
        }
        if path in queries:
            return http.reply(200, queries[path]())
        load_test = LOAD_TEST_RESOURCE.fullmatch(path)
        if load_test and load_test[2] is None:
            return http.reply(200, self.preliminary.load_test_progress(unquote(load_test[1])))
        manual_duel = MANUAL_DUEL_RESOURCE.fullmatch(path)
        if manual_duel:
            return http.reply(200, self.duels.show(unquote(manual_duel[1]), manual=True))
        replay = REPLAY_RESOURCE.fullmatch(path)
        if replay:
            return self.replay(http, unquote(replay[1]))
        if path == '/admin/prelim/results.xlsx':
            return self.workbook(http, self.preliminary.export_results(), '初赛结果.xlsx', 'prelim-results.xlsx')
        if path == '/admin/arena/status':
            status = self.arena.status() if self.arena is not None else None
            if status is None:
                raise ContestError(502, 'ARENA_STATUS_UNAVAILABLE', 'Arena 状态暂不可用')
            return http.reply(200, status)
        raise ContestError(404, 'NOT_FOUND', '接口不存在')

    def replay(self, http, run_id):
        self.duels.require_replay(run_id)
        try:
            if self.archive_root is None:
                raise FileNotFoundError()
            path = self.archive_root.resolve() / 'runs' / run_id / 'replay.json'
            if path.resolve(strict=True) != path or not path.is_file():
                raise FileNotFoundError()
            data = path.read_bytes()
        except (OSError, RuntimeError) as error:
            raise ContestError(404, 'REPLAY_NOT_FOUND', '该局暂无可播放的回放') from error
        return http.respond(200, data, 'application/json; charset=utf-8')

    def post(self, http, path):
        if path == '/admin/prelim/plan':
            selection = None
            if http.headers.get('Content-Length', '0') != '0':
                selection = http.read_json('INVALID_TEAM_SELECTION', '请配置参赛队伍')
            return http.reply(200, self.preliminary.plan(selection))
        if path == '/admin/prelim/confirm':
            return http.reply(200, self.preliminary.confirm())
        if path == '/admin/prelim/roster':
            data = http.read_body(MAX_UPLOAD, 'INVALID_ROSTER', '请选择 XLSX 队伍名单')
            return http.reply(200, self.preliminary.import_roster(data))
        if path == '/admin/prelim/retry':
            return self.retry(http)
        if path == '/admin/prelim/withdraw':
            body = http.read_json('INVALID_WITHDRAWAL', '请选择退赛队伍')
            return http.reply(200, self.preliminary.withdraw(body.get('teams')))
        if path == '/admin/duels':
            body = http.read_json('INVALID_DUEL', '请选择规则并填写双方队伍编号')
            return http.reply(202, self.duels.accept_manual(body))
        if path == '/admin/check':
            body = http.read_json('INVALID_CHECK', '请填写 Bot 地址和当前规则（prelim）')
            return http.reply(200, check_bot(body))
        if path == '/admin/rulings':
            body = http.read_json('INVALID_RULING', '请填写裁定范围、编号、胜方和理由')
            ruling = self.rulings.record(body.get('scope'), body.get('target'), body.get('winner'),
                                         body.get('reason'), operator=self.user)
            return http.reply(200, ruling)
        load_test = LOAD_TEST_RESOURCE.fullmatch(path)
        if load_test:
            batch_id = unquote(load_test[1])
            if load_test[2] == 'retry':
                return self.retry(http, load_test_id=batch_id)
            if load_test[2] == 'stop':
                return http.reply(200, self.preliminary.stop_load_test(batch_id))
            rules = None
            if http.headers.get_content_type() == 'application/json':
                data = http.read_json('INVALID_TEAM_SELECTION', '请配置压测队伍')
                if 'rules' in data:
                    rules = data.pop('rules')
                    if rules != RULESET:
                        raise ContestError(400, 'INVALID_RULES', '批量对战须使用当前赛题规则（prelim）')
            else:
                data = http.read_body(MAX_UPLOAD, 'INVALID_ROSTER', '请选择 XLSX 压测名单')
            return http.reply(200, self.preliminary.start_load_test(batch_id, data, rules=rules))
        raise ContestError(404, 'NOT_FOUND', '接口不存在')

    def retry(self, http, *, load_test_id=None):
        body = http.read_json('INVALID_RUN_ID', '请选择要重跑的执行')
        run_id = body.get('run_id')
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ContestError(400, 'INVALID_RUN_ID', '请选择要重跑的执行')
        return http.reply(200, self.preliminary.retry(run_id, load_test_id=load_test_id))


def handler_for(contest, *, admin=None, ip_whitelist=None):
    whitelist = IPWhitelist(ip_whitelist)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type=None, headers=None):
            self.send_response(status)
            if content_type is not None:
                self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            if headers:
                for name, value in headers.items():
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def reply(self, status, payload=None, headers=None):
            if payload is None:
                self.respond(status, b'', headers=headers)
            else:
                self.respond(status, dumps(payload).encode('utf-8'), 'application/json; charset=utf-8', headers)

        def read_body(self, limit, code, message):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= limit:
                    raise ValueError()
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError()
                return data
            except ValueError as error:
                raise ContestError(400, code, message) from error

        def read_json(self, code, message, limit=65536):
            data = self.read_body(limit, code, message)
            try:
                return json_object(data)
            except (ValueError, RecursionError) as error:
                raise ContestError(400, code, message) from error

        def dispatch(self, method):
            path = self.path.partition('?')[0] if method == 'GET' else self.path
            try:
                if path == '/admin' or path.startswith('/admin/'):
                    if admin is None:
                        raise ContestError(404, 'NOT_FOUND', '接口不存在')
                    if not whitelist.allows(self.client_address[0]):
                        raise ContestError(403, 'IP_NOT_ALLOWED', '当前 IP 不在后台访问白名单中')
                    return admin.handle(self, method)
                if method == 'GET':
                    route = DUEL_QUERY.fullmatch(path)
                    if route is not None:
                        duel_id, resource = route.groups()
                        query = contest.duels.status if resource == 'status' else contest.duels.result
                        return self.reply(200, query(unquote(duel_id)))
                elif path == '/contest/v1/duels':
                    body = self.read_json('INVALID_JSON', '请求必须包含 UTF-8 JSON 对象')
                    return self.reply(202, contest.duels.accept(body))
                return self.reply(404, {'code': 'NOT_FOUND'})
            except ContestError as error:
                headers = None
                if error.status == 401:
                    headers = {'WWW-Authenticate': 'Basic realm="Contest", charset="UTF-8"'}
                self.reply(error.status, {'code': error.code, 'message': str(error)}, headers)

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

    return Handler


class IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(settings):
    settings = settings.for_server()
    arena = ArenaClient(settings.arena_url)
    pools = load_pools(settings.maps)
    settings.db.parent.mkdir(parents=True, exist_ok=True)
    stop = Event()
    contest = Contest(settings.db, pools, phase=settings.phase, shutdown=stop)
    admin = (Admin(contest, settings.admin_user, settings.admin_password, arena, archive_root=settings.archive_root)
             if settings.admin_user else None)
    worker = ContestWorker(contest, settings.archive_root, arena)

    handler = handler_for(contest, admin=admin, ip_whitelist=settings.ip_whitelist)
    bind_ip = ip_address(settings.host)
    server_type = IPv6HTTPServer if bind_ip.version == 6 else ThreadingHTTPServer
    with server_type((settings.host, settings.port), handler) as server:
        contest.runs.recover_submissions()
        local_ip = ip_address('::1' if bind_ip.version == 6 else '127.0.0.1') if bind_ip.is_unspecified else bind_ip
        local_host = f'[{local_ip}]' if local_ip.version == 6 else str(local_ip)
        address = f'http://{local_host}:{server.server_port}'
        print(f'Contest {address}{"/admin/prelim" if admin else ""}', flush=True)
        listen_host = f'[{bind_ip}]' if bind_ip.version == 6 else str(bind_ip)
        print(f'Listening http://{listen_host}:{server.server_port}', flush=True)
        print(f'Arena {settings.arena_url}', flush=True)
        with worker.running(stop):
            server.serve_forever()
