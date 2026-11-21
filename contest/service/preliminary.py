"""Round-robin batches, official results and isolated load-test progress."""
from datetime import datetime
from itertools import combinations
import json
import secrets
from uuid import uuid4

from .domain import DUEL_RULING_LABEL, DUEL_TIE_ISSUE, LOAD_TEST_ID, OFFICIAL_BATCH, TEAM_ID, ZONE, ContestError, encode
from .duels import EffectiveGame, FORMAL_GAMES, forfeit_result, formal_maps, insert_plan, save_result
from .executions import retry_attempt
from .workbook import DUEL_HEADERS, GAME_HEADERS, RANK_HEADERS, WorkbookError, read_roster, write_workbook


_GAME_STATES = ('pending', 'waiting_result', 'waiting_review', 'awaiting_action',
                'unconfirmed', 'effective', 'waived')


def _now():
    return datetime.now(ZONE).isoformat(timespec='microseconds')


def _batch(connection, batch_id=OFFICIAL_BATCH):
    return connection.execute('SELECT * FROM prelim_batches WHERE batch_id = ?', (batch_id,)).fetchone()


def _planned_batch(connection):
    batch = _batch(connection)
    if batch is None or batch['planned_at'] is None:
        raise ContestError(409, 'PRELIM_NOT_PLANNED', '请先创建初赛单循环')
    return batch


def _settled_batch(connection):
    batch = _planned_batch(connection)
    pending = connection.execute('''
        SELECT 1 FROM duels WHERE batch_id = ? AND result IS NULL LIMIT 1
    ''', (batch['batch_id'],)).fetchone()
    if pending is not None:
        raise ContestError(409, 'PRELIM_INCOMPLETE', '初赛单循环还有未结算的对决')
    return batch


def _withdrawn_teams(connection, batch_id):
    return {row['team_id'] for row in connection.execute(
        'SELECT team_id FROM prelim_withdrawals WHERE batch_id = ?', (batch_id,),
    )}


def _read_teams(data):
    try:
        records = read_roster(data)
    except WorkbookError as error:
        raise ContestError(400, 'INVALID_ROSTER', str(error)) from error
    teams, seen = [], set()
    for row, team, name in records:
        if not TEAM_ID.fullmatch(team):
            raise ContestError(400, 'INVALID_ROSTER', f'「队伍」第 {row} 行的队伍编号应为 team 加三位数字')
        if team in seen:
            raise ContestError(400, 'INVALID_ROSTER', f'「队伍」第 {row} 行的队伍编号 {team} 重复')
        seen.add(team)
        teams.append({'team_id': team, 'name': name})
    if not teams:
        raise ContestError(400, 'INVALID_ROSTER', '「队伍」表没有参赛队伍')
    return teams


def _plan_batch(connection, batch, pools):
    batch_id = batch['batch_id']
    prefix = 'prelim' if batch_id == OFFICIAL_BATCH else 'load'
    teams = [team['team_id'] for team in json.loads(batch['roster'])]
    maps = formal_maps(pools, 'prelim') if len(teams) > 1 else ()
    for a, b in combinations(teams, 2):
        sides = [a, b] if secrets.randbelow(2) == 0 else [b, a]
        body = {'duel_id': f'duel-{prefix}-{uuid4().hex}', 'rules': 'prelim', 'sides': sides}
        insert_plan(connection, 'prelim', body, maps, batch_id=batch_id)
    connection.execute('UPDATE prelim_batches SET planned_at = ? WHERE batch_id = ?', (_now(), batch_id))


def _validate_load_test_id(batch_id):
    if not isinstance(batch_id, str) or not LOAD_TEST_ID.fullmatch(batch_id):
        raise ContestError(400, 'INVALID_LOAD_TEST_ID', '压测编号格式为 load- 加 1 至 40 位字母、数字、下划线或连字符')


def _load_batch(connection, batch_id):
    batch = _batch(connection, batch_id)
    if batch is None:
        raise ContestError(404, 'LOAD_TEST_NOT_FOUND', '压测批次不存在')
    return batch


def _game_state(row):
    if row['review'] == 'effective':
        return 'effective'
    if row['settled']:
        return 'waived'
    if row['closed_at'] is not None:
        return 'cancelled'
    if row['review'] in ('held', 'void', 'build_failed', 'awaiting_ruling') or row['submission'] == 'rejected':
        return 'awaiting_action'
    if row['has_result']:
        return 'waiting_review'
    if row['submission'] in ('unconfirmed', 'resend'):
        return 'unconfirmed'
    if row['submission'] in ('sending', 'accepted'):
        return 'waiting_result'
    return 'pending'


def _progress(connection, batch_id=OFFICIAL_BATCH):
    official = batch_id == OFFICIAL_BATCH
    batch = _batch(connection, batch_id) if official else _load_batch(connection, batch_id)
    games = dict.fromkeys(_GAME_STATES, 0)
    duels = {'total': 0, 'settled': 0, 'awaiting_action': 0, 'pending': 0}
    if not official:
        games['cancelled'] = duels['cancelled'] = 0
    attention = []
    blocked = set()
    last_settled = ''
    for row in connection.execute('''
        SELECT duel_id, result IS NOT NULL AS settled, closed_at, awaiting_ruling
        FROM duels WHERE batch_id = ? ORDER BY id
    ''', (batch_id,)):
        duels['total'] += 1
        if row['settled']:
            duels['settled'] += 1
            last_settled = max(last_settled, row['closed_at'])
        elif row['closed_at'] is not None:
            duels['cancelled'] += 1
        elif row['awaiting_ruling']:
            blocked.add(row['duel_id'])
            attention.append({'duel_id': row['duel_id'], 'issue': DUEL_TIE_ISSUE})

    for row in connection.execute('''
        SELECT d.duel_id, d.result IS NOT NULL AS settled, d.closed_at, g.ordinal, r.run_id, r.submission,
               r.review, r.issue, r.arena_result IS NOT NULL AS has_result
        FROM duels d JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
        WHERE d.batch_id = ? ORDER BY d.id, g.ordinal
    ''', (batch_id,)):
        state = _game_state(row)
        games[state] += 1
        if state in ('awaiting_action', 'unconfirmed') or (state == 'waiting_result' and row['issue']):
            blocked.add(row['duel_id'])
            issue = '构建结论已确认，等待前局处理' if row['review'] == 'build_failed' else row['issue'] or '交付未确认'
            attention.append({'duel_id': row['duel_id'], 'run_id': row['run_id'], 'ordinal': row['ordinal'],
                              'issue': issue, 'retryable': row['review'] == 'void'})
    duels['awaiting_action'] = len(blocked)
    duels['pending'] = duels['total'] - duels['settled'] - duels['awaiting_action'] - duels.get('cancelled', 0)
    games['total'] = sum(games.values())

    if batch is None:
        state = 'empty'
    elif batch['stopped_at'] is not None:
        state = 'stopped'
    elif batch['confirmed_at'] is not None:
        state = 'confirmed'
    elif batch['planned_at'] is None:
        state = 'roster_imported'
    elif duels['total'] == duels['settled']:
        state = 'awaiting_confirmation' if official else 'finished'
    else:
        state = 'running'

    roster = json.loads(batch['roster']) if batch else []
    withdrawn = _withdrawn_teams(connection, batch_id)
    for team in roster:
        team['withdrawn'] = team['team_id'] in withdrawn
    attempts = connection.execute('''
        SELECT COALESCE(SUM(r.attempt > 1), 0) AS retries, COALESCE(SUM(r.review = 'void'), 0) AS voided
        FROM duels d JOIN games g ON g.duel = d.id JOIN runs r ON r.game = g.id WHERE d.batch_id = ?
    ''', (batch_id,)).fetchone()
    result = {'state': state, 'teams': roster, 'duels': duels, 'games': games,
              'attempts': dict(attempts), 'attention': attention}
    if official:
        result['confirmed_at'] = batch['confirmed_at'] if batch else None
    else:
        planned_at = batch['planned_at']
        stopped_at = batch['stopped_at']
        finished_at = last_settled if state == 'finished' else None
        elapsed = (datetime.fromisoformat(finished_at or stopped_at or _now())
                   - datetime.fromisoformat(planned_at)).total_seconds()
        result.update(batch_id=batch_id, planned_at=planned_at, finished_at=finished_at, stopped_at=stopped_at,
                      elapsed_seconds=round(max(0, elapsed), 3),
                      duels_per_minute=round(60 * duels['settled'] / elapsed, 2) if elapsed > 0 else 0)
    return result


def _result_reason(result, ruled):
    if ruled:
        return DUEL_RULING_LABEL.format(result['reason'])
    if 'reason' in result:
        return result['reason']
    if 'build_run_id' in result:
        return '构建双败' if result['outcome'] == 'double_loss' else '构建失败'
    return result.get('tiebreak', '胜局数')


def _summarize_results(connection, batch, *, include_details=False):
    wins = dict.fromkeys((team['team_id'] for team in json.loads(batch['roster'])), 0)
    losses = wins.copy()
    duel_rows, effective = [], set()
    for row in connection.execute('''
        SELECT d.duel_id, d.submission, d.result, d.ruling IS NOT NULL AS ruled
        FROM duels d WHERE d.batch_id = ? ORDER BY d.id
    ''', (batch['batch_id'],)):
        a, b = json.loads(row['submission'])['sides']
        result = json.loads(row['result'])
        won, lost = result['games_won'], result['games_lost']
        for team in (a, b):
            wins[team] += won[team]
            losses[team] += lost[team]
        if include_details:
            duel_id = row['duel_id']
            duel_rows.append((duel_id, a, b, won[a], lost[a], won[b], lost[b], result['outcome'],
                              result['winner'], _result_reason(result, row['ruled']), result.get('build_run_id')))
            effective.update((duel_id, run_id) for run_id in result['runs'])

    order = sorted(wins, key=lambda team: (-wins[team], losses[team], team))
    ranking = [(rank, team, wins[team], losses[team]) for rank, team in enumerate(order, 1)]
    return ranking, duel_rows, effective


def _game_rows(connection, batch_id, effective):
    """Stream the confirmed game rows inside the export's read transaction."""
    for row in connection.execute('''
        SELECT d.duel_id, g.ordinal, r.run_id, r.manifest, r.ruling FROM duels d
        JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
        WHERE d.batch_id = ? AND r.review = 'effective' ORDER BY d.id, g.ordinal
    ''', (batch_id,)):
        key = row['duel_id'], row['run_id']
        if key in effective:
            game = EffectiveGame.from_row(row)
            yield row['duel_id'], game.ordinal, game.run_id, game.winner, game.ruling['reason'] if game.ruling else None
            effective.remove(key)
    if effective:
        raise ContestError(500, 'PRELIM_RUN_MISSING', '已确认的赛果引用了缺失的有效执行')


class Preliminary:
    def __init__(self, database, maps):
        self.database, self.maps = database, maps

    def import_roster(self, data):
        teams = _read_teams(data)
        with self.database.transaction() as connection:
            batch = _batch(connection)
            if batch is not None and batch['planned_at'] is not None:
                if teams != json.loads(batch['roster']):
                    raise ContestError(409, 'ROSTER_FROZEN', '单循环已创建，参赛名单已冻结')
            else:
                connection.execute('''
                    INSERT INTO prelim_batches (batch_id, roster) VALUES (?, ?)
                    ON CONFLICT (batch_id) DO UPDATE SET roster = excluded.roster
                ''', (OFFICIAL_BATCH, encode(teams)))
            return _progress(connection)

    def plan(self):
        with self.database.transaction() as connection:
            batch = _batch(connection)
            if batch is None:
                raise ContestError(409, 'ROSTER_REQUIRED', '请先导入截止时的参赛名单')
            if batch['planned_at'] is None:
                _plan_batch(connection, batch, self.maps)
            return _progress(connection)

    def progress(self):
        with self.database.transaction(write=False) as connection:
            return _progress(connection)

    def retry(self, run_id, *, load_test_id=None):
        if load_test_id is not None:
            _validate_load_test_id(load_test_id)
        batch_id = OFFICIAL_BATCH if load_test_id is None else load_test_id
        with self.database.transaction() as connection:
            replacement = retry_attempt(connection, run_id, batch_id=batch_id)
            return dict(_progress(connection, batch_id), retry_run_id=replacement)

    def start_load_test(self, batch_id, data):
        _validate_load_test_id(batch_id)
        teams = _read_teams(data)
        if len(teams) < 2:
            raise ContestError(400, 'INVALID_ROSTER', '压测名单至少需要两支队伍')
        with self.database.transaction() as connection:
            batch = _batch(connection, batch_id)
            if batch is None:
                connection.execute('INSERT INTO prelim_batches (batch_id, roster) VALUES (?, ?)',
                                   (batch_id, encode(teams)))
                _plan_batch(connection, _batch(connection, batch_id), self.maps)
            elif teams != json.loads(batch['roster']):
                raise ContestError(409, 'LOAD_TEST_CONFLICT', '同一压测编号已有不同的队伍名单')
            return _progress(connection, batch_id)

    def load_test_progress(self, batch_id):
        _validate_load_test_id(batch_id)
        with self.database.transaction(write=False) as connection:
            return _progress(connection, batch_id)

    def stop_load_test(self, batch_id):
        _validate_load_test_id(batch_id)
        with self.database.transaction() as connection:
            batch = _load_batch(connection, batch_id)
            if batch['stopped_at'] is None:
                stopped_at = _now()
                updated = connection.execute('''
                    UPDATE duels SET closed_at = ?, awaiting_ruling = 0
                    WHERE batch_id = ? AND closed_at IS NULL
                ''', (stopped_at, batch_id))
                if updated.rowcount:
                    connection.execute('UPDATE prelim_batches SET stopped_at = ? WHERE batch_id = ?',
                                       (stopped_at, batch_id))
            return _progress(connection, batch_id)

    def load_tests(self):
        with self.database.transaction(write=False) as connection:
            rows = connection.execute('''
                SELECT batch_id, planned_at FROM prelim_batches
                WHERE batch_id != ? ORDER BY planned_at DESC, batch_id
            ''', (OFFICIAL_BATCH,))
            return {'batches': [dict(row) for row in rows]}

    def withdraw(self, teams):
        if not isinstance(teams, list) or not teams or any(not isinstance(team, str) for team in teams):
            raise ContestError(400, 'INVALID_WITHDRAWAL', '请选择退赛队伍')
        with self.database.transaction() as connection:
            batch = _planned_batch(connection)
            if batch['confirmed_at'] is not None:
                raise ContestError(409, 'PRELIM_CONFIRMED', '初赛赛果已确认')
            registered = {team['team_id'] for team in json.loads(batch['roster'])}
            if not set(teams) <= registered:
                raise ContestError(400, 'INVALID_WITHDRAWAL', '退赛队伍不在初赛名单中')

            timestamp = _now()
            connection.executemany('INSERT OR IGNORE INTO prelim_withdrawals VALUES (?, ?, ?)',
                                   ((OFFICIAL_BATCH, team, timestamp) for team in teams))
            withdrawn = _withdrawn_teams(connection, OFFICIAL_BATCH)
            unstarted = connection.execute('''
                SELECT d.id, d.submission FROM duels d
                WHERE d.batch_id = ? AND d.result IS NULL AND NOT EXISTS (
                    SELECT 1 FROM games g JOIN runs r ON r.game = g.id
                    WHERE g.duel = d.id AND r.submission != 'pending'
                ) ORDER BY d.id
            ''', (OFFICIAL_BATCH,))
            for row in unstarted:
                sides = json.loads(row['submission'])['sides']
                defeated = withdrawn.intersection(sides)
                if defeated:
                    result = forfeit_result(sides, FORMAL_GAMES, defeated, [])
                    save_result(connection, row['id'], dict(result, reason='退赛'))
            return _progress(connection)

    def confirm(self):
        with self.database.transaction() as connection:
            batch = _settled_batch(connection)
            if batch['confirmed_at'] is None:
                connection.execute('UPDATE prelim_batches SET confirmed_at = ? WHERE batch_id = ?',
                                   (_now(), OFFICIAL_BATCH))
            return _progress(connection)

    def standings(self):
        with self.database.transaction(write=False) as connection:
            batch = _settled_batch(connection)
            ranking, _, _ = _summarize_results(connection, batch)
            return {'rows': ranking}

    def export_results(self):
        with self.database.transaction(write=False) as connection:
            batch = _batch(connection)
            if batch is None or batch['confirmed_at'] is None:
                raise ContestError(409, 'PRELIM_NOT_CONFIRMED', '请先确认初赛赛果')
            ranking, duel_rows, effective = _summarize_results(connection, batch, include_details=True)
            sheets = (('排名', RANK_HEADERS, ranking, len(ranking)),
                      ('对决', DUEL_HEADERS, duel_rows, len(duel_rows)),
                      ('对局', GAME_HEADERS, _game_rows(connection, OFFICIAL_BATCH, effective), len(effective)))
            try:
                return write_workbook(sheets)
            except WorkbookError as error:
                raise ContestError(409, 'PRELIM_EXPORT_LIMIT', str(error)) from error
