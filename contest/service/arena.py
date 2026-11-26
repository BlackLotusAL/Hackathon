"""Contest's Arena requests, run tracking and archive review."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from http.client import HTTPConnection, HTTPException
import json
from threading import Thread
from time import monotonic, time
from typing import NamedTuple
from urllib.parse import urlsplit

from engine import VERSION
from engine.maps import load_map
from engine.protocol import RUN_ID, dumps, json_object
from engine.replay import VerificationError, check_fields, game_result, read_referee_log, verify_replay
from engine.rules import RULESET

from .database import DatabaseStopping
from .domain import ACTIVE_DUELS, DUEL_PHASE, GAME_TIE_ISSUE, ZONE, ContestError, encode
from .duels import insert_attempt, settle_duel


ACTIVE_ARENA_STATES = ('queued', 'running')
PLATFORM_ERRORS = ('judge_crash', 'wall_timeout', 'container_start_failed', 'slot_unhealthy', 'build_platform_error')
RUN_QUERY_BATCH_SIZE = 100


def parse_peer_url(base_url):
    if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
        raise ValueError('内部服务地址必须为 HTTP 基础 URL')
    url = urlsplit(base_url)
    if (url.scheme != 'http' or not url.hostname or url.username is not None
            or url.query or url.fragment or (url.port is not None and url.port == 0)):
        raise ValueError('内部服务地址必须为 HTTP 基础 URL')
    return url


class ArenaClient:
    """Call Arena's submission, run query and capacity endpoints."""

    def __init__(self, base_url):
        url = parse_peer_url(base_url)
        self.host, self.port, self.prefix = url.hostname, url.port, url.path.rstrip('/')

    def _request(self, method, path, body=None):
        client = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = dumps(body).encode('utf-8')
        try:
            client.request(method, self.prefix + path, body, headers)
            with client.getresponse() as response:
                data = response.read(65537) if response.status == 200 else None
                if data and len(data) <= 65536:
                    try:
                        return response.status, json_object(data)
                    except (ValueError, RecursionError):
                        pass
                return response.status, None
        except (OSError, HTTPException):
            return None, None
        finally:
            client.close()

    def submit(self, request):
        status, _ = self._request('POST', '/arena/v1/runs', request)
        return status

    def query_runs(self, run_ids):
        if not 1 <= len(run_ids) <= RUN_QUERY_BATCH_SIZE:
            raise ValueError('Arena 执行查询须包含 1 至 100 个执行编号')
        return self._request('POST', '/arena/v1/runs/query', {'run_ids': list(run_ids)})

    def status(self):
        _, payload = self._request('GET', '/arena/v1/status')
        return payload


def run_observation(run_id, payload):
    if not isinstance(payload, dict) or payload.get('run_id') != run_id:
        return None
    state = payload.get('state')
    if state == 'platform_error':
        if set(payload) == {'run_id', 'state', 'reason'} and payload['reason'] in PLATFORM_ERRORS:
            return {'state': state, 'reason': payload['reason']}
    elif state in (*ACTIVE_ARENA_STATES, 'finished', 'build_failed') and set(payload) == {'run_id', 'state'}:
        return {'state': state}
    return None


def run_query_results(run_ids, payload):
    """Return the response by ID, with None values for explicitly missing executions."""
    if not isinstance(payload, dict):
        return None
    runs, missing = payload.get('runs'), payload.get('missing_run_ids')
    if not isinstance(runs, list) or not isinstance(missing, list):
        return None
    remaining, results = set(run_ids), {}
    for run in runs:
        run_id = run.get('run_id') if isinstance(run, dict) else None
        if not isinstance(run_id, str) or run_id not in remaining:
            return None
        remaining.remove(run_id)
        results[run_id] = run
    for run_id in missing:
        if not isinstance(run_id, str) or run_id not in remaining:
            return None
        remaining.remove(run_id)
        results[run_id] = None
    return results if not remaining else None


POLL_INTERVAL = 0.5
POLL_LEASE = 15

_OPEN_RUNS = f'''
    FROM current_runs r JOIN games g ON g.id = r.game JOIN duels d ON d.id = g.duel
    WHERE d.closed_at IS NULL AND {ACTIVE_DUELS}
'''
_POLLABLE_RUNS = _OPEN_RUNS + '''
    AND r.submission IN ('accepted', 'unconfirmed') AND r.arena_result IS NULL AND r.review IS NULL
'''
_BATCH_POLL = "(d.batch_id IS NOT NULL OR d.kind = 'prelim')"


class PollClaim(NamedTuple):
    run_id: str
    expires_at: float


def _reserve_polls(connection, rows, now):
    claims = tuple(PollClaim(row['run_id'], now + POLL_LEASE) for row in rows)
    connection.executemany('UPDATE runs SET poll_after = ? WHERE run_id = ?',
                           ((claim.expires_at, claim.run_id) for claim in claims))
    return claims


def _poll_values(run, status, results, now):
    run_id = run['run_id']
    payload = results[run_id] if results is not None else None
    observed = run_observation(run_id, payload)
    result = None
    if observed is not None:
        submission, failures, issue = 'accepted', 0, None
        if observed['state'] not in ACTIVE_ARENA_STATES:
            result = encode(observed)
    else:
        submission, issue = run['submission'], run['issue']
        failures = min(run['poll_failures'] + 1, 6)
        missing = results is not None and payload is None
        if missing and submission == 'accepted':
            issue = 'Arena 未找到已受理的执行，请核对执行记录'
        elif status == 200 and not missing:
            issue = 'Arena 返回的执行编号、状态或故障原因无效'
        elif status is not None and 400 <= status < 500 and status not in (408, 429):
            issue = f'Arena 执行查询失败（HTTP {status}）'

    if run['batch_poll']:
        poll_after, failures = 0, 0
    else:
        delay = min(30, 2 ** (failures - 1)) if failures else POLL_INTERVAL
        poll_after = now + delay
    return submission, result, poll_after, failures, issue, run_id


def _request(row):
    body = json.loads(row['duel_submission'])
    return {'run_id': row['run_id'], 'duel_id': body['duel_id'], 'rules': body['rules'],
            'bots': json.loads(row['bots']), 'config': {'map': json.loads(row['map'])}}


def _submission_candidates(connection, limit):
    active = connection.execute(f'''
        SELECT {DUEL_PHASE} AS phase {_OPEN_RUNS}
        AND r.submission IN ('sending', 'accepted', 'unconfirmed', 'resend') AND r.review IS NULL
        ORDER BY d.id LIMIT 1
    ''').fetchone()
    if active is None:
        active = connection.execute(f'''
            SELECT {DUEL_PHASE} AS phase FROM duels d
            WHERE d.closed_at IS NULL AND {ACTIVE_DUELS} ORDER BY d.id LIMIT 1
        ''').fetchone()
    if active is None:
        return []
    return connection.execute(f'''
        SELECT r.run_id, d.submission AS duel_submission, g.bots, g.map {_OPEN_RUNS}
        AND r.submission IN ('pending', 'resend') AND r.arena_result IS NULL AND r.review IS NULL
        AND ({DUEL_PHASE}) = ?
        AND NOT EXISTS (
            SELECT 1 FROM games earlier JOIN current_runs previous ON previous.game = earlier.id
            WHERE earlier.duel = d.id AND earlier.ordinal < g.ordinal AND previous.review = 'build_failed'
        )
        ORDER BY g.ordinal, d.id LIMIT ?
    ''', (active['phase'], limit)).fetchall()


def _open_attempt(connection, run_id):
    run = connection.execute(
        f'SELECT r.*, g.duel, d.batch_id {_OPEN_RUNS} AND r.run_id = ?', (run_id,),
    ).fetchone()
    if run is None:
        raise ContestError(409, 'ATTEMPT_CLOSED', '该执行已被替代或对决已关闭')
    return run


def retry_attempt(connection, run_id, *, batch_id=None):
    """Create a replacement inside the caller's transaction."""
    run = _open_attempt(connection, run_id)
    if batch_id is not None and run['batch_id'] != batch_id:
        raise ContestError(404, 'BATCH_RUN_NOT_FOUND', '执行不属于当前批次')
    if run['review'] != 'void':
        raise ContestError(409, 'NOT_VOID', '该执行尚未作废')
    return insert_attempt(connection, run['game'], run['attempt'] + 1)


class ExecutionService:
    """Track Contest submissions, Arena observations and local review decisions."""

    def __init__(self, database):
        self.database = database

    def claim(self):
        requests = self.claim_submissions(1)
        return requests[0] if requests else None

    def has_ready_submissions(self):
        with self.database.transaction(write=False) as connection:
            return bool(_submission_candidates(connection, 1))

    def claim_submissions(self, limit):
        with self.database.transaction() as connection:
            rows = _submission_candidates(connection, limit)
            connection.executemany("UPDATE runs SET submission = 'sending' WHERE run_id = ?",
                                   ((row['run_id'],) for row in rows))
            return [_request(row) for row in rows]

    def complete_submission(self, run_id, status):
        issue = None
        if status == 202:
            submission = 'accepted'
        elif status is not None and 400 <= status < 500 and status not in (408, 429):
            submission, issue = 'rejected', f'Arena 拒绝执行请求（HTTP {status}），请核对原任务'
        else:
            submission = 'unconfirmed'
        with self.database.transaction() as connection:
            connection.execute(f'''
                UPDATE runs SET submission = ?, issue = ?, poll_after = 0, poll_failures = 0
                WHERE run_id IN (
                    SELECT r.run_id {_OPEN_RUNS} AND r.run_id = ? AND r.submission = 'sending' AND r.review IS NULL
                )
            ''', (submission, issue, run_id))

    def recover_submissions(self):
        """Mark interrupted submissions for status queries."""
        with self.database.transaction() as connection:
            connection.execute(f'''
                UPDATE runs SET submission = 'unconfirmed'
                WHERE run_id IN (SELECT r.run_id {_OPEN_RUNS} AND r.submission = 'sending' AND r.review IS NULL)
            ''')

    def claim_poll(self, *, now=None):
        now = time() if now is None else now
        with self.database.transaction() as connection:
            run = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS}
                AND NOT {_BATCH_POLL} AND r.poll_after <= ?
                ORDER BY r.poll_after, d.id, g.ordinal LIMIT 1
            ''', (now,)).fetchone()
            if run is not None:
                return _reserve_polls(connection, (run,), now)[0]

    def preliminary_poll_targets(self):
        """Snapshot the eligible executions at the start of a batch polling round."""
        with self.database.transaction(write=False) as connection:
            rows = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS} AND {_BATCH_POLL}
                ORDER BY d.id, g.ordinal
            ''')
            return [row['run_id'] for row in rows]

    def claim_preliminary_polls(self, run_ids, *, now=None):
        now = time() if now is None else now
        placeholders = ', '.join('?' for _ in run_ids)
        with self.database.transaction() as connection:
            rows = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS} AND {_BATCH_POLL}
                AND r.poll_after <= ? AND r.run_id IN ({placeholders})
                ORDER BY d.id, g.ordinal
            ''', (now, *run_ids)).fetchall()
            return _reserve_polls(connection, rows, now)

    def complete_polls(self, claims, status, payload, *, now=None):
        now = time() if now is None else now
        expires = {claim.run_id: claim.expires_at for claim in claims}
        results = run_query_results(expires, payload) if status == 200 else None
        placeholders = ', '.join('?' for _ in expires)
        with self.database.transaction() as connection:
            rows = connection.execute(f'''
                SELECT r.run_id, r.submission, r.poll_after, r.poll_failures, r.issue,
                       {_BATCH_POLL} AS batch_poll {_POLLABLE_RUNS}
                AND r.run_id IN ({placeholders})
            ''', tuple(expires)).fetchall()
            # Apply responses only to attempts still reserved by this query.
            updates = (_poll_values(run, status, results, now)
                       for run in rows if run['poll_after'] == expires[run['run_id']])
            connection.executemany('''
                UPDATE runs SET submission = ?, arena_result = ?, poll_after = ?, poll_failures = ?, issue = ?
                WHERE run_id = ?
            ''', updates)

    def next_review(self):
        with self.database.transaction(write=False) as connection:
            row = connection.execute(f'''
                SELECT r.run_id, r.arena_result, d.kind, d.submission AS duel_submission, g.bots, g.map {_OPEN_RUNS}
                AND r.arena_result IS NOT NULL AND r.review IS NULL ORDER BY d.id, g.ordinal LIMIT 1
            ''').fetchone()
            if row is not None:
                return _request(row), json.loads(row['arena_result']), row['kind'].endswith('-ladder')

    def complete_review(self, run_id, review, *, manifest=None, issue=None):
        with self.database.transaction() as connection:
            duel = connection.execute(f'''
                SELECT d.id, d.kind, d.commits, d.started_at {_OPEN_RUNS}
                AND r.run_id = ? AND r.arena_result IS NOT NULL AND r.review IS NULL
            ''', (run_id,)).fetchone()
            if duel is None:
                return
            if manifest is not None:
                if (review == 'effective' and manifest['result']['winner'] is None
                        and manifest['result'].get('reason') != 'double_loss'):
                    review, issue = 'awaiting_ruling', GAME_TIE_ISSUE
                pinned = duel['commits']
                commits = json.loads(pinned) if pinned is not None else {}
                reported = {bot['team_id']: bot['commit'] for bot in manifest['bots'] if 'commit' in bot}
                updated_commits = encode({**commits, **reported})
                started = manifest.get('duel_started_at') if duel['kind'].endswith('-ladder') else None
                if pinned is not None and (duel['started_at'] != started or any(
                        team in commits and commits[team] != commit for team, commit in reported.items())):
                    review, issue = 'held', '同场执行的提交版本或天梯开始时间不一致'
                elif pinned != updated_commits:
                    connection.execute('UPDATE duels SET commits = ?, started_at = ? WHERE id = ?',
                                       (updated_commits, started, duel['id']))
            connection.execute('UPDATE runs SET review = ?, issue = ?, manifest = ? WHERE run_id = ?',
                               (review, issue, encode(manifest) if manifest is not None else None, run_id))
            if review in ('effective', 'build_failed'):
                settle_duel(connection, duel['id'])

    def recheck(self, run_id):
        with self.database.transaction() as connection:
            run = _open_attempt(connection, run_id)
            if run['review'] != 'held':
                raise ContestError(409, 'NOT_HELD', '该执行当前未暂缓生效')
            connection.execute('UPDATE runs SET review = NULL, issue = NULL WHERE run_id = ?', (run_id,))

    def invalidate(self, run_id, reason):
        if not reason.strip():
            raise ContestError(400, 'REASON_REQUIRED', '须记录作废原因')
        with self.database.transaction() as connection:
            run = _open_attempt(connection, run_id)
            if run['submission'] == 'pending':
                raise ContestError(409, 'RUN_NOT_SUBMITTED', '该执行尚未提交')
            connection.execute("UPDATE runs SET review = 'void', issue = ? WHERE run_id = ?", (reason, run_id))
            connection.execute('UPDATE duels SET awaiting_ruling = 0 WHERE id = ?', (run['duel'],))

    def retry(self, run_id):
        with self.database.transaction() as connection:
            return retry_attempt(connection, run_id)

    def resend(self, run_id):
        with self.database.transaction() as connection:
            result = connection.execute(f'''
                UPDATE runs SET submission = 'resend', issue = NULL, poll_after = 0, poll_failures = 0
                WHERE run_id IN (
                    SELECT r.run_id {_OPEN_RUNS} AND r.run_id = ? AND r.submission IN ('unconfirmed', 'rejected')
                    AND r.arena_result IS NULL AND r.review IS NULL
                )
            ''', (run_id,))
            if result.rowcount != 1:
                raise ContestError(409, 'NOT_UNCONFIRMED', '该执行请求当前无法补发')


def review_archive(root, request, state, *, ladder):
    """Verify Arena's published evidence before it can participate in settlement."""
    run_id = request['run_id']
    if not RUN_ID.fullmatch(run_id):
        raise VerificationError('执行编号无效')
    path = root / 'runs' / run_id
    try:
        if request['rules'] != RULESET:
            raise VerificationError('对局须使用当前赛题规则')
        manifest = json_object((path / 'match.json').read_bytes())
        encode(manifest)  # Reject non-finite values before recording the evidence.
        bots = manifest['bots']
        if [bot['team_id'] for bot in bots] != request['bots']:
            raise VerificationError('归档队伍与预定出生点不一致')
        for bot in bots:
            commit = bot.get('commit')
            if 'commit' in bot and (not isinstance(commit, str) or not commit.strip()):
                raise VerificationError(f"归档中 {bot['team_id']} 的提交号无效")
        if (bots[0]['team_id'] == bots[1]['team_id'] and all('commit' in bot for bot in bots)
                and bots[0]['commit'] != bots[1]['commit']):
            raise VerificationError('同一参赛方在两个出生点的提交版本不一致')
        if ladder:
            started = manifest['duel_started_at']
            if not started.endswith('+08:00') or datetime.fromisoformat(started).utcoffset() != ZONE.utcoffset(None):
                raise VerificationError('天梯开始时间必须使用 +08:00')

        if state == 'build_failed':
            statuses = [bot['state'] for bot in bots]
            if 'result' in manifest or 'failed' not in statuses or any(
                    mark not in ('succeeded', 'failed') for mark in statuses):
                raise VerificationError('归档缺少双方已确认的构建失败结论')
            return manifest

        if state != 'finished' or any('state' in bot for bot in bots):
            raise VerificationError('归档与执行状态不符')
        result = manifest.get('result')
        if not isinstance(result, dict) or result.get('run_id') != run_id:
            raise VerificationError(f'match.json 的 result.run_id 缺失或与完整执行编号 {run_id} 不一致')
        version = result.get('version')
        if version != VERSION:
            raise VerificationError(f'单局清单的版本不受支持（{version!r}）')
        board, digest = load_map(request['config']['map'])
        check_fields(result, {'map': board.id, 'map_sha256': digest}, 'match.json')
        replay = json_object((path / 'replay.json').read_bytes())
        log = read_referee_log(path / 'log.jsonl')
        game = verify_replay(replay, board=board, log=log)
        # If Arena omits the settled round count, use the verified replay.
        result.setdefault('turns', game.turn)
        check_fields(result, game_result(game), 'match.json')
        return manifest
    except VerificationError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError) as error:
        raise VerificationError(f'无法复核 {run_id} 的归档 ({error})') from error


PRELIMINARY_POLL_INTERVAL = 10
ARENA_SUBMIT_WORKERS = 12
WORK_IDLE_INTERVAL = 0.25


class ContestWorker:
    """Submit and poll runs in Arena and review the published archives."""

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

    @contextmanager
    def running(self, stop):
        """Start and join the Contest workers with a shared shutdown event."""
        def work(step):
            while not stop.is_set():
                if not step():
                    stop.wait(WORK_IDLE_INTERVAL)

        def run_worker(task, *args):
            try:
                task(*args)
            except DatabaseStopping:
                return

        steps = (self.poll_next, self.review_next)
        workers = [Thread(target=run_worker, args=(work, step), daemon=True, name=step.__name__) for step in steps]
        workers.extend(Thread(target=run_worker, args=(task, stop), daemon=True, name=task.__name__)
                       for task in (self.submit_available, self.poll_preliminary))
        started = []
        try:
            for thread in workers:
                thread.start()
                started.append(thread)
            yield
        finally:
            stop.set()
            for thread in started:
                thread.join()
