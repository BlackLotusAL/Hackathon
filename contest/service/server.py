"""Contest HTTP endpoints, organizer access and service lifecycle."""
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
import hmac
import re
import socket
from threading import Event, Thread
from urllib.parse import quote, unquote

from engine.protocol import dumps, json_object

from .application import Contest, ContestWorker
from .arena import ArenaClient
from .config import IPWhitelist
from .database import DatabaseStopping
from .domain import RUN_ID, ContestError
from .duels import load_pools
from .workbook import MAX_UPLOAD, MIME


DUEL_QUERY = re.compile(r'/contest/v1/duels/([^/]+)/(status|result)')
LOAD_TEST_RESOURCE = re.compile(r'/admin/load-tests/([^/]+)(?:/(retry|stop))?')


class Admin:
    def __init__(self, contest, user, password, arena=None):
        self.preliminary, self.rulings = contest.preliminary, contest.rulings
        self.user, self.arena = user, arena
        self.credentials = b64encode(f'{user}:{password}'.encode('utf-8'))
        self.page = Path(__file__).with_name('admin.html').read_bytes()

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
        path = http.path.partition('?')[0]
        if method == 'GET':
            self.get(http, path)
        else:
            self.post(http, path)

    def get(self, http, path):
        if path in ('/admin', '/admin/prelim', '/admin/prelim/'):
            return http.respond(200, self.page, 'text/html; charset=utf-8')
        queries = {
            '/admin/prelim/progress': self.preliminary.progress,
            '/admin/prelim/standings': self.preliminary.standings,
            '/admin/load-tests': self.preliminary.load_tests,
            '/admin/rulings': self.rulings.pending,
            '/admin/final/forfeits': self.rulings.forfeitable_finals,
        }
        if path in queries:
            return http.reply(200, queries[path]())
        load_test = LOAD_TEST_RESOURCE.fullmatch(path)
        if load_test and load_test[2] is None:
            return http.reply(200, self.preliminary.load_test_progress(unquote(load_test[1])))
        if path == '/admin/prelim/results.xlsx':
            return self.workbook(http, self.preliminary.export_results(), '初赛结果.xlsx', 'prelim-results.xlsx')
        if path == '/admin/arena/status':
            status = self.arena.status() if self.arena is not None else None
            if status is None:
                raise ContestError(502, 'ARENA_STATUS_UNAVAILABLE', 'Arena 状态暂不可用')
            return http.reply(200, status)
        raise ContestError(404, 'NOT_FOUND', '接口不存在')

    def post(self, http, path):
        commands = {
            '/admin/prelim/plan': self.preliminary.plan,
            '/admin/prelim/confirm': self.preliminary.confirm,
        }
        if path in commands:
            return http.reply(200, commands[path]())
        if path == '/admin/prelim/roster':
            data = http.read_body(MAX_UPLOAD, 'INVALID_ROSTER', '请选择 XLSX 队伍名单')
            return http.reply(200, self.preliminary.import_roster(data))
        if path == '/admin/prelim/retry':
            return self.retry(http)
        if path == '/admin/prelim/withdraw':
            body = http.read_json('INVALID_WITHDRAWAL', '请选择退赛队伍')
            return http.reply(200, self.preliminary.withdraw(body.get('teams')))
        if path == '/admin/rulings':
            body = http.read_json('INVALID_RULING', '请填写裁定范围、编号、胜方和理由')
            ruling = self.rulings.record(body.get('scope'), body.get('target'), body.get('winner'),
                                         body.get('reason'), operator=self.user)
            return http.reply(200, ruling)
        if path == '/admin/final/forfeits':
            body = http.read_json('INVALID_FORFEIT', '请填写决赛场次、判负队伍和理由')
            ruling = self.rulings.forfeit_final(body.get('duel_id'), body.get('defeated'), body.get('reason'),
                                                operator=self.user)
            return http.reply(200, ruling)
        load_test = LOAD_TEST_RESOURCE.fullmatch(path)
        if load_test:
            batch_id = unquote(load_test[1])
            if load_test[2] == 'retry':
                return self.retry(http, load_test_id=batch_id)
            if load_test[2] == 'stop':
                return http.reply(200, self.preliminary.stop_load_test(batch_id))
            data = http.read_body(MAX_UPLOAD, 'INVALID_ROSTER', '请选择 XLSX 压测名单')
            return http.reply(200, self.preliminary.start_load_test(batch_id, data))
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
                    contest.duels.accept(body)
                    return self.reply(202)
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
    contest = Contest(settings.db, pools, shutdown=stop)
    admin = (Admin(contest, settings.admin_user, settings.admin_password, arena)
             if settings.admin_user else None)
    worker = ContestWorker(contest, settings.archive_root, arena)

    def work(step):
        while not stop.is_set():
            if not step():
                stop.wait(0.25)

    def run_worker(task, *args):
        try:
            task(*args)
        except DatabaseStopping:
            return

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
        steps = (worker.submit_next, worker.poll_next, worker.review_next)
        workers = [Thread(target=run_worker, args=(work, step), daemon=True, name=step.__name__) for step in steps]
        workers.append(Thread(target=run_worker, args=(worker.poll_preliminary, stop),
                              daemon=True, name='poll_preliminary'))
        for thread in workers:
            thread.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            for thread in workers:
                thread.join()
