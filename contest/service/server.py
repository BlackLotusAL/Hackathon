"""Contest HTTP endpoints, organizer access and service lifecycle."""
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
import hmac
import re
import socket
from threading import Event, Thread
from time import monotonic
from urllib.parse import parse_qs, quote, unquote

from engine.protocol import RUN_ID, dumps, json_object
from engine.verification import VerificationError

from .arena import RUN_QUERY_BATCH_SIZE, ArenaClient, ExecutionService, review_archive
from .config import IPWhitelist
from .database import Database, DatabaseStopping
from .domain import BATTLE_RULES, OFFICIAL_BATCH, ContestError
from .duels import DuelService, Rulings, load_pools
from .preliminary import Preliminary
from .self_check import check_bot
from .workbook import MAX_UPLOAD, MIME


PRELIMINARY_POLL_INTERVAL = 10
ARENA_SUBMIT_WORKERS = 12
WORK_IDLE_INTERVAL = 0.25
DUEL_QUERY = re.compile(r'/contest/v1/duels/([^/]+)/(status|result)')
LOAD_TEST_RESOURCE = re.compile(r'/admin/load-tests/([^/]+)(?:/(retry|stop))?')
MANUAL_DUEL_RESOURCE = re.compile(r'/admin/duels/([^/]+)(?:/(stop))?')
REPLAY_RESOURCE = re.compile(r'/admin/runs/([^/]+)/replay')


class Contest:
    """One shared database, with explicit feature services for every caller."""

    def __init__(self, path, pools=None, *, phase='prelim-ladder', shutdown=None):
        self.database = Database(path, shutdown=shutdown)
        self.maps = pools or {}
        self.duels = DuelService(self.database, self.maps, phase)
        self.runs = ExecutionService(self.database)
        self.preliminary = Preliminary(self.database, self.maps, phase)
        self.rulings = Rulings(self.database)


class ContestWorker:
    def __init__(self, contest, archive_root, arena):
        self.contest, self.archive_root = contest, archive_root
        self.arena = arena

    def submit_next(self):
        request = self.contest.runs.claim()
        if request is None:
            return False
        self._submit(request)
        return True

    def _submit(self, request):
        status = self.arena.submit(request)
        self.contest.runs.complete_submission(request['run_id'], status)

    def _submission_capacity(self):
        status = self.arena.status() or {}
        slots = status.get('slots')
        idle = slots.get('idle') if isinstance(slots, dict) else None
        if type(idle) is not int or idle < 0:
            return 1  # Keep dispatching serially when slot status is unavailable.
        queued = status.get('queue', 0)
        if type(queued) is not int or queued < 0:
            return min(1, idle)
        return min(ARENA_SUBMIT_WORKERS, max(0, idle - queued))

    def submit_available(self, stop):
        runs = self.contest.runs
        submitting = set()
        with ThreadPoolExecutor(max_workers=ARENA_SUBMIT_WORKERS, thread_name_prefix='arena-submit') as executor:
            while not stop.is_set():
                completed = {future for future in submitting if future.done()}
                for future in completed:
                    future.result()
                submitting.difference_update(completed)

                if len(submitting) < ARENA_SUBMIT_WORKERS and runs.has_ready_submissions():
                    # In-flight submissions may not be counted in Arena's snapshot yet.
                    limit = self._submission_capacity() - len(submitting)
                    if limit > 0 and not stop.is_set():
                        submitting.update(executor.submit(self._submit, request)
                                          for request in runs.claim_submissions(limit))
                stop.wait(WORK_IDLE_INTERVAL)

    def poll_next(self):
        claim = self.contest.runs.claim_poll()
        if claim is None:
            return False
        self._poll_runs((claim,))
        return True

    def _poll_runs(self, claims):
        status, payload = self.arena.query_runs([claim.run_id for claim in claims])
        self.contest.runs.complete_polls(claims, status, payload)

    def poll_preliminary_round(self, stop=None):
        targets = self.contest.runs.preliminary_poll_targets()
        for offset in range(0, len(targets), RUN_QUERY_BATCH_SIZE):
            if stop is not None and stop.is_set():
                return
            batch = targets[offset:offset + RUN_QUERY_BATCH_SIZE]
            claims = self.contest.runs.claim_preliminary_polls(batch)
            if claims:
                self._poll_runs(claims)

    def poll_preliminary(self, stop):
        next_round = monotonic()
        while not stop.wait(max(0, next_round - monotonic())):
            self.poll_preliminary_round(stop)
            # Advance to the next scheduled tick, skipping ticks consumed by this round.
            elapsed = monotonic() - next_round
            intervals = int(elapsed // PRELIMINARY_POLL_INTERVAL) + 1
            next_round += intervals * PRELIMINARY_POLL_INTERVAL

    def review_next(self):
        pending = self.contest.runs.next_review()
        if pending is None:
            return False
        request, arena_result, ladder = pending
        run_id, state = request['run_id'], arena_result['state']
        if state == 'platform_error':
            self.contest.runs.complete_review(run_id, 'void', issue=arena_result['reason'])
            return True
        try:
            manifest = review_archive(self.archive_root, request, state, ladder=ladder)
        except VerificationError as error:
            self.contest.runs.complete_review(run_id, 'held', issue=str(error))
        else:
            review = 'effective' if state == 'finished' else 'build_failed'
            self.contest.runs.complete_review(run_id, review, manifest=manifest)
        return True


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
        if manual_duel and manual_duel[2] is None:
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
            body = http.read_json('INVALID_CHECK', '请选择规则并填写 Bot 地址')
            return http.reply(200, check_bot(body))
        manual_duel = MANUAL_DUEL_RESOURCE.fullmatch(path)
        if manual_duel and manual_duel[2] == 'stop':
            return http.reply(200, self.duels.stop_manual(unquote(manual_duel[1])))
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
                    if rules not in BATTLE_RULES:
                        raise ContestError(400, 'INVALID_RULES', '请选择初赛或决赛规则')
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

    def work(step):
        while not stop.is_set():
            if not step():
                stop.wait(WORK_IDLE_INTERVAL)

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
        steps = (worker.poll_next, worker.review_next)
        workers = [Thread(target=run_worker, args=(work, step), daemon=True, name=step.__name__) for step in steps]
        workers.extend(Thread(target=run_worker, args=(task, stop), daemon=True, name=task.__name__)
                       for task in (worker.submit_available, worker.poll_preliminary))
        for thread in workers:
            thread.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            for thread in workers:
                thread.join()
