"""Arena execution lifecycle, archive review and operator recovery."""
from datetime import datetime
import json
from time import time
from typing import NamedTuple

from engine.mapgen import load_map
from engine.protocol import json_object
from engine.replay import LEGACY_ENGINE_VERSION, VerificationError, check_fields, game_result, verify_replay

from .arena import ACTIVE_ARENA_STATES, batch_lookups, run_observation
from .domain import BASELINES, RUN_ID, ZONE, ContestError, canonical_id, encode
from .duels import insert_attempt, settle_duel


POLL_INTERVAL = 0.5
POLL_LEASE = 15

_OPEN_RUNS = '''
    FROM current_runs r JOIN games g ON g.id = r.game JOIN duels d ON d.id = g.duel
    WHERE d.result IS NULL
'''
_POLLABLE_RUNS = _OPEN_RUNS + '''
    AND r.submission IN ('accepted', 'unconfirmed') AND r.arena_result IS NULL AND r.review IS NULL
'''


class PollClaim(NamedTuple):
    run_id: str
    expires_at: float


def _reserve_polls(connection, rows, now):
    claims = tuple(PollClaim(row['run_id'], now + POLL_LEASE) for row in rows)
    connection.executemany('UPDATE runs SET poll_after = ? WHERE run_id = ?',
                           ((claim.expires_at, claim.run_id) for claim in claims))
    return claims


def _poll_values(run, lookup, now, preliminary, query_error):
    status, payload = lookup
    observed = run_observation(run['run_id'], payload) if status == 200 else None
    result = None
    if observed is not None:
        submission, failures, issue = 'accepted', 0, None
        if observed['state'] not in ACTIVE_ARENA_STATES:
            result = encode(observed)
    else:
        submission, issue = run['submission'], run['issue']
        failures = min(run['poll_failures'] + 1, 6)
        if query_error is not None:
            issue = query_error
        elif status == 404 and submission == 'accepted':
            issue = 'Arena 未找到已受理的执行，请核对执行记录'
        elif status == 200:
            issue = 'Arena 返回的执行编号、状态或故障原因无效'
        elif status is not None and 400 <= status < 500 and status not in (404, 408, 429):
            issue = f'Arena 执行查询失败（HTTP {status}）'

    if preliminary:
        poll_after, failures = 0, 0
    else:
        delay = min(30, 2 ** (failures - 1)) if failures else POLL_INTERVAL
        poll_after = now + delay
    return submission, result, poll_after, failures, issue, run['run_id']


def _request(row):
    body = json.loads(row['duel_submission'])
    return {'run_id': row['run_id'], 'duel_id': body['duel_id'], 'rules': body['rules'],
            'bots': json.loads(row['bots']), 'config': {'map': json.loads(row['map'])}}


def _open_attempt(connection, run_id):
    run = connection.execute(
        f'SELECT r.*, g.duel {_OPEN_RUNS} AND r.run_id = ?', (run_id,),
    ).fetchone()
    if run is None:
        raise ContestError(409, 'ATTEMPT_CLOSED', '该执行已被替代或整场已结算')
    return run


def retry_attempt(connection, run_id, *, preliminary=False):
    """Create a replacement inside the caller's transaction."""
    run = _open_attempt(connection, run_id)
    if preliminary and connection.execute(
            'SELECT 1 FROM prelim_duels WHERE duel = ?', (run['duel'],),
    ).fetchone() is None:
        raise ContestError(404, 'PRELIM_RUN_NOT_FOUND', '执行不属于本次初赛')
    if run['review'] != 'void':
        raise ContestError(409, 'NOT_VOID', '该执行尚未作废')
    return insert_attempt(connection, run['game'], run['attempt'] + 1)


class ExecutionService:
    def __init__(self, database):
        self.database = database

    def claim(self):
        with self.database.transaction() as connection:
            active = connection.execute(f'''
                SELECT d.id, d.kind {_OPEN_RUNS}
                AND r.submission != 'pending' AND r.review IS NULL ORDER BY d.id LIMIT 1
            ''').fetchone()
            kind = active['kind'] if active else None
            final_duel = active['id'] if kind == 'final' else None
            row = connection.execute(f'''
                SELECT r.run_id, d.submission AS duel_submission, g.bots, g.map {_OPEN_RUNS}
                AND r.submission IN ('pending', 'resend') AND r.arena_result IS NULL AND r.review IS NULL
                AND (? IS NULL OR d.kind = ?) AND (? IS NULL OR d.id = ?)
                AND NOT EXISTS (
                    SELECT 1 FROM games earlier JOIN current_runs previous ON previous.game = earlier.id
                    WHERE earlier.duel = d.id AND earlier.ordinal < g.ordinal AND previous.review IS NOT 'effective'
                )
                ORDER BY d.id, g.ordinal LIMIT 1
            ''', (kind, kind, final_duel, final_duel)).fetchone()
            if row is not None:
                connection.execute("UPDATE runs SET submission = 'sending' WHERE run_id = ?", (row['run_id'],))
                return _request(row)

    def complete_submission(self, run_id, status):
        issue = None
        if status == 202:
            submission = 'accepted'
        elif status is not None and 400 <= status < 500 and status not in (408, 429):
            submission, issue = 'rejected', f'Arena 拒绝执行请求（HTTP {status}），请核对原任务'
        else:
            submission = 'unconfirmed'
        with self.database.transaction() as connection:
            connection.execute('''
                UPDATE runs SET submission = ?, issue = ?, poll_after = 0, poll_failures = 0
                WHERE run_id = ? AND submission = 'sending' AND review IS NULL
            ''', (submission, issue, run_id))

    def recover_submissions(self):
        with self.database.transaction() as connection:
            connection.execute("UPDATE runs SET submission = 'unconfirmed' WHERE submission = 'sending'")

    def claim_poll(self, *, now=None):
        now = time() if now is None else now
        with self.database.transaction() as connection:
            run = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS}
                AND d.kind != 'prelim' AND r.poll_after <= ?
                ORDER BY r.poll_after, d.id, g.ordinal LIMIT 1
            ''', (now,)).fetchone()
            if run is not None:
                return _reserve_polls(connection, (run,), now)[0]

    def preliminary_poll_targets(self):
        """Snapshot the eligible executions at the start of a preliminary round."""
        with self.database.transaction(write=False) as connection:
            rows = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS} AND d.kind = 'prelim'
                ORDER BY d.id, g.ordinal
            ''')
            return [row['run_id'] for row in rows]

    def claim_preliminary_polls(self, run_ids, *, now=None):
        now = time() if now is None else now
        placeholders = ', '.join('?' for _ in run_ids)
        with self.database.transaction() as connection:
            rows = connection.execute(f'''
                SELECT r.run_id {_POLLABLE_RUNS} AND d.kind = 'prelim'
                AND r.poll_after <= ? AND r.run_id IN ({placeholders})
                ORDER BY d.id, g.ordinal
            ''', (now, *run_ids)).fetchall()
            return _reserve_polls(connection, rows, now)

    def complete_poll(self, claim, status, payload, *, now=None):
        self._complete_polls((claim,), {claim.run_id: (status, payload)}, now=now)

    def complete_preliminary_polls(self, claims, status, payload, *, now=None):
        lookups = batch_lookups((claim.run_id for claim in claims), payload) if status == 200 else {}
        default = (200, None) if status == 200 else (None, None)
        query_error = None
        if status is not None and 400 <= status < 500 and status not in (408, 429):
            query_error = f'Arena 批量查询失败（HTTP {status}）'
        self._complete_polls(claims, lookups, now=now, preliminary=True, default=default, query_error=query_error)

    def _complete_polls(self, claims, lookups, *, now, preliminary=False, default=(None, None), query_error=None):
        now = time() if now is None else now
        expires = {claim.run_id: claim.expires_at for claim in claims}
        placeholders = ', '.join('?' for _ in expires)
        with self.database.transaction() as connection:
            rows = connection.execute(f'''
                SELECT r.run_id, r.submission, r.poll_after, r.poll_failures, r.issue {_POLLABLE_RUNS}
                AND r.run_id IN ({placeholders})
            ''', tuple(expires)).fetchall()
            # Apply responses only to attempts still reserved by this query.
            updates = (_poll_values(run, lookups.get(run['run_id'], default), now, preliminary, query_error)
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
                commits = encode({bot['team_id']: bot['commit'] for bot in manifest['bots'] if 'commit' in bot})
                started = manifest.get('duel_started_at') if duel['kind'].endswith('-ladder') else None
                if duel['commits'] is None:
                    connection.execute('UPDATE duels SET commits = ?, started_at = ? WHERE id = ?',
                                       (commits, started, duel['id']))
                elif duel['commits'] != commits or duel['started_at'] != started:
                    review, issue = 'held', '同场执行的提交版本或天梯开始时间不一致'
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
            connection.execute('UPDATE duels SET issue = NULL WHERE id = ?', (run['duel'],))

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
    """Verify immutable evidence before it can participate in settlement."""
    run_id = request['run_id']
    if not RUN_ID.fullmatch(run_id):
        raise VerificationError('执行编号无效')
    path = root / 'runs' / run_id
    try:
        manifest = json_object((path / 'match.json').read_bytes())
        encode(manifest)  # Reject non-finite values before recording the evidence.
        bots = manifest['bots']
        if [bot['team_id'] for bot in bots] != request['bots']:
            raise VerificationError('归档队伍与预定出生点不一致')
        for bot in bots:
            commit = bot.get('commit')
            if canonical_id(bot['team_id']) not in BASELINES or 'commit' in bot:
                if not isinstance(commit, str) or not commit:
                    raise VerificationError('归档缺少有效提交号')
        if ladder:
            started = manifest['duel_started_at']
            if not started.endswith('+08:00') or datetime.fromisoformat(started).utcoffset() != ZONE.utcoffset(None):
                raise VerificationError('天梯开始时间必须使用 +08:00')

        if state == 'build_failed':
            statuses = [bot['state'] for bot in bots]
            if 'result' in manifest or 'failed' not in statuses or any(
                    mark not in ('succeeded', 'failed') for mark in statuses):
                raise VerificationError('归档缺少双方已确认的构建失败结论')
            return manifest, None

        if state != 'finished' or any('state' in bot for bot in bots):
            raise VerificationError('归档与执行状态不符')
        result = manifest['result']
        version = result['engine_version']
        board, digest = load_map(request['config']['map'], request['rules'])
        check_fields(result, {'map': board['id'], 'map_sha256': digest}, 'match.json')
        replay = json_object((path / 'replay.json').read_bytes())
        with (path / 'log.jsonl').open('rb') as stream:
            log = [json_object(line) for line in stream]
        game = verify_replay(replay, engine_version=version, board=board, log=log,
                             require_public=not ladder and version != LEGACY_ENGINE_VERSION)
        recomputed = game_result(game)
        if 'turns' not in result:
            recomputed.pop('turns')
        check_fields(result, recomputed, 'match.json')
        pending = game.winner is None or getattr(game, 'decided_by_seat', False)
        return manifest, '单局四项指标全同，待主办方处理' if pending else None
    except VerificationError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError) as error:
        raise VerificationError(f'无法复核 {run_id} 的归档 ({error})') from error
