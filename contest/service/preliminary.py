"""Preliminary roster, round-robin administration and result delivery."""
from datetime import datetime
from itertools import combinations
import json
import secrets
from uuid import uuid4

from .domain import TEAM_ID, ZONE, ContestError
from .duels import FORMAL_GAMES, forfeit_result, formal_maps, insert_plan, save_result
from .executions import retry_attempt
from .workbook import DUEL_HEADERS, GAME_HEADERS, RANK_HEADERS, WorkbookError, read_roster, write_workbook


_GAME_STATES = ('pending', 'waiting_result', 'waiting_review', 'awaiting_action',
                'unconfirmed', 'effective', 'waived')


def _now():
    return datetime.now(ZONE).isoformat(timespec='seconds')


def _batch(connection):
    return connection.execute('SELECT * FROM prelim_batch WHERE id = 1').fetchone()


def _planned_batch(connection):
    batch = _batch(connection)
    if batch is None or batch['planned_at'] is None:
        raise ContestError(409, 'PRELIM_NOT_PLANNED', '请先创建初赛单循环')
    return batch


def _settled_batch(connection):
    batch = _planned_batch(connection)
    pending = connection.execute('''
        SELECT 1 FROM prelim_duels pd JOIN duels d ON d.id = pd.duel
        WHERE d.result IS NULL LIMIT 1
    ''').fetchone()
    if pending is not None:
        raise ContestError(409, 'PRELIM_INCOMPLETE', '初赛单循环还有未结算的对决')
    return batch


def _withdrawn_teams(connection):
    return {row['team_id'] for row in connection.execute('SELECT team_id FROM prelim_withdrawals')}


def _game_state(row):
    if row['review'] == 'effective':
        return 'effective'
    if row['settled']:
        return 'waived'
    if row['review'] in ('held', 'void', 'build_failed') or row['submission'] == 'rejected':
        return 'awaiting_action'
    if row['has_result']:
        return 'waiting_review'
    if row['submission'] in ('unconfirmed', 'resend'):
        return 'unconfirmed'
    if row['submission'] in ('sending', 'accepted'):
        return 'waiting_result'
    return 'pending'


def _progress(connection):
    batch = _batch(connection)
    games = dict.fromkeys(_GAME_STATES, 0)
    duels = {'total': 0, 'settled': 0, 'awaiting_action': 0, 'pending': 0}
    attention = []
    blocked = set()
    for row in connection.execute('''
        SELECT d.duel_id, d.result IS NOT NULL AS settled, d.issue
        FROM prelim_duels pd JOIN duels d ON d.id = pd.duel ORDER BY d.id
    '''):
        settled = row['settled']
        duels['total'] += 1
        duels['settled'] += settled
        if row['issue'] and not settled:
            blocked.add(row['duel_id'])
            attention.append({'duel_id': row['duel_id'], 'issue': row['issue']})

    for row in connection.execute('''
        SELECT d.duel_id, d.result IS NOT NULL AS settled, g.ordinal, r.run_id, r.submission,
               r.review, r.issue, r.arena_result IS NOT NULL AS has_result
        FROM prelim_duels pd JOIN duels d ON d.id = pd.duel JOIN games g ON g.duel = d.id
        JOIN current_runs r ON r.game = g.id ORDER BY d.id, g.ordinal
    '''):
        state = _game_state(row)
        games[state] += 1
        if state in ('awaiting_action', 'unconfirmed') or (state == 'waiting_result' and row['issue']):
            blocked.add(row['duel_id'])
            issue = '构建结论已确认，等待前局处理' if row['review'] == 'build_failed' else row['issue'] or '交付未确认'
            attention.append({'duel_id': row['duel_id'], 'run_id': row['run_id'], 'ordinal': row['ordinal'],
                              'issue': issue, 'retryable': row['review'] == 'void'})
    duels['awaiting_action'] = len(blocked)
    duels['pending'] = duels['total'] - duels['settled'] - duels['awaiting_action']
    games['total'] = sum(games.values())

    if batch is None:
        state = 'empty'
    elif batch['confirmed_at'] is not None:
        state = 'confirmed'
    elif batch['planned_at'] is None:
        state = 'roster_imported'
    elif duels['total'] == duels['settled']:
        state = 'awaiting_confirmation'
    else:
        state = 'running'

    roster = json.loads(batch['roster']) if batch else []
    withdrawn = _withdrawn_teams(connection)
    for team in roster:
        team['withdrawn'] = team['team_id'] in withdrawn
    attempts = connection.execute('''
        SELECT COALESCE(SUM(r.attempt > 1), 0) AS retries, COALESCE(SUM(r.review = 'void'), 0) AS voided
        FROM prelim_duels pd JOIN games g ON g.duel = pd.duel JOIN runs r ON r.game = g.id
    ''').fetchone()
    return {'state': state, 'teams': roster, 'confirmed_at': batch['confirmed_at'] if batch else None,
            'duels': duels, 'games': games, 'attempts': dict(attempts), 'attention': attention}


def _result_reason(result):
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
        SELECT d.duel_id, d.submission, d.result
        FROM prelim_duels pd JOIN duels d ON d.id = pd.duel ORDER BY d.id
    '''):
        a, b = json.loads(row['submission'])['sides']
        result = json.loads(row['result'])
        won, lost = result['games_won'], result['games_lost']
        for team in (a, b):
            wins[team] += won[team]
            losses[team] += lost[team]
        if include_details:
            duel_id = row['duel_id']
            duel_rows.append((duel_id, a, b, won[a], lost[a], won[b], lost[b], result['outcome'],
                              result['winner'], _result_reason(result), result.get('build_run_id')))
            effective.update((duel_id, run_id) for run_id in result['runs'])

    order = sorted(wins, key=lambda team: (-wins[team], losses[team], team))
    ranking = [(rank, team, wins[team], losses[team]) for rank, team in enumerate(order, 1)]
    return ranking, duel_rows, effective


def _game_rows(connection, effective):
    """Stream the confirmed game rows inside the export's read transaction."""
    for row in connection.execute('''
        SELECT d.duel_id, g.ordinal, r.run_id FROM prelim_duels pd JOIN duels d ON d.id = pd.duel
        JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
        WHERE r.review = 'effective' ORDER BY d.id, g.ordinal
    '''):
        key = row['duel_id'], row['run_id']
        if key in effective:
            yield tuple(row)
            effective.remove(key)
    if effective:
        raise ContestError(500, 'PRELIM_RUN_MISSING', '已确认的赛果引用了缺失的有效执行')


class Preliminary:
    def __init__(self, database, maps):
        self.database, self.maps = database, maps

    def import_roster(self, data):
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

        with self.database.transaction() as connection:
            batch = _batch(connection)
            if batch is not None and batch['planned_at'] is not None:
                if teams != json.loads(batch['roster']):
                    raise ContestError(409, 'ROSTER_FROZEN', '单循环已创建，参赛名单已冻结')
            else:
                connection.execute('''
                    INSERT INTO prelim_batch (id, roster) VALUES (1, ?)
                    ON CONFLICT (id) DO UPDATE SET roster = excluded.roster
                ''', (json.dumps(teams, ensure_ascii=False),))
            return _progress(connection)

    def plan(self):
        with self.database.transaction() as connection:
            batch = _batch(connection)
            if batch is None:
                raise ContestError(409, 'ROSTER_REQUIRED', '请先导入截止时的参赛名单')
            if batch['planned_at'] is None:
                teams = [team['team_id'] for team in json.loads(batch['roster'])]
                maps = formal_maps(self.maps, 'prelim') if len(teams) > 1 else ()
                for a, b in combinations(teams, 2):
                    sides = [a, b] if secrets.randbelow(2) == 0 else [b, a]
                    body = {'duel_id': f'duel-prelim-{uuid4().hex}', 'rules': 'prelim', 'sides': sides}
                    duel = insert_plan(connection, 'prelim', body, maps)
                    connection.execute('INSERT INTO prelim_duels (duel) VALUES (?)', (duel,))
                connection.execute('UPDATE prelim_batch SET planned_at = ? WHERE id = 1', (_now(),))
            return _progress(connection)

    def progress(self):
        with self.database.transaction(write=False) as connection:
            return _progress(connection)

    def retry(self, run_id):
        with self.database.transaction() as connection:
            replacement = retry_attempt(connection, run_id, preliminary=True)
            return dict(_progress(connection), retry_run_id=replacement)

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
            connection.executemany('INSERT OR IGNORE INTO prelim_withdrawals VALUES (?, ?)',
                                   ((team, timestamp) for team in teams))
            withdrawn = _withdrawn_teams(connection)
            unstarted = connection.execute('''
                SELECT d.id, d.submission FROM prelim_duels pd JOIN duels d ON d.id = pd.duel
                WHERE d.result IS NULL AND NOT EXISTS (
                    SELECT 1 FROM games g JOIN runs r ON r.game = g.id
                    WHERE g.duel = d.id AND r.submission != 'pending'
                ) ORDER BY d.id
            ''')
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
                connection.execute('UPDATE prelim_batch SET confirmed_at = ? WHERE id = 1', (_now(),))
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
                      ('对局', GAME_HEADERS, _game_rows(connection, effective), len(effective)))
            try:
                return write_workbook(sheets)
            except WorkbookError as error:
                raise ContestError(409, 'PRELIM_EXPORT_LIMIT', str(error)) from error
