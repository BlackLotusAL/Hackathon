"""Duel planning, settlement, rulings and Portal queries."""
from datetime import datetime, timedelta
import json
import secrets
from typing import NamedTuple, Optional
from uuid import uuid4

from engine.rules import PARAMS, RULESET, TIEBREAK_METRICS, compare_metrics
from engine.mapgen import load_map
from engine.protocol import RUN_ID, json_object

from .domain import (
    ACTIVE_DUELS, BASELINES, DUEL_ID, DUEL_KINDS, DUEL_RULING_LABEL, DUEL_TIE_ISSUE,
    LADDER_PHASE, LOAD_TEST_ID, MANUAL_KIND, OFFICIAL_BATCH, TEAM_ID, ZONE, ContestError, encode,
    require_phase, require_phase_available,
)
from .workbook import fits_text_cell


FORMAL_MAPS = 3
LADDER_GAMES = 2
FORMAL_GAMES = FORMAL_MAPS * 2
REPLAY_REVIEWS = ('effective', 'awaiting_ruling')
BATCH_REPLAY_PAGE_SIZE = 25


def load_pools(path):
    """Load sealed map pools and verify their published hashes."""
    pools = json_object(path.read_bytes()) if path else {}
    result = {}
    for stage, entries in pools.items():
        if stage != RULESET:
            raise ValueError('地图池配置包含未知赛段')
        if not isinstance(entries, list) or len(entries) != FORMAL_MAPS:
            raise ValueError('每个正式地图池必须包含三张地图')
        specs, ids = [], set()
        for entry in entries:
            raw = (path.parent / entry['file']).read_bytes().decode('utf-8')
            spec = {'file': raw, 'sha256': entry['sha256']}
            board, _ = load_map(spec, stage)
            ids.add(board.id)
            specs.append(spec)
        if len(ids) != len(entries):
            raise ValueError('正式地图编号重复')
        result[stage] = specs
    return result


def formal_maps(pools):
    maps = pools.get(RULESET)
    if not maps:
        raise ContestError(503, 'MAP_POOL_UNAVAILABLE', '尚未配置该赛段的正式地图池')
    return maps


def _sides(body, *, allow_baseline=True, allow_same=False, first_baseline=False):
    if not isinstance(body, dict) or body.keys() != {'sides'}:
        raise ContestError(400, 'INVALID_DUEL', '请求体须只包含 sides')
    sides = body['sides']
    if not isinstance(sides, list) or len(sides) != 2 or not all(isinstance(side, str) for side in sides):
        raise ContestError(400, 'INVALID_DUEL', 'sides 须包含两个编号')
    first, second = sides
    first_ok = TEAM_ID.fullmatch(first) or (first_baseline and first in BASELINES)
    second_ok = TEAM_ID.fullmatch(second) or (allow_baseline and second in BASELINES)
    if (first == second and not allow_same) or not first_ok or not second_ok:
        raise ContestError(400, 'INVALID_DUEL', '双方编号无效')
    return sides


def insert_attempt(connection, game, attempt):
    run_id = f'run-{game}.{attempt}'
    connection.execute('INSERT INTO runs (run_id, game, attempt) VALUES (?, ?, ?)', (run_id, game, attempt))
    return run_id


def insert_plan(connection, kind, body, maps, *, batch_id=None, first_bots=None):
    """Persist the duel, its immutable game plan and the first attempts together."""
    duel = connection.execute(
        'INSERT INTO duels (duel_id, submission, kind, batch_id, accepted_at) VALUES (?, ?, ?, ?, ?)',
        (body['duel_id'], encode(body), kind, batch_id, datetime.now(ZONE).isoformat(timespec='microseconds')),
    ).lastrowid
    if first_bots is None:
        first_bots = body['sides']
    sides = (encode(first_bots), encode(first_bots[::-1]))
    ordinal = 0
    for spec in maps:
        map_payload = encode(spec)
        for bots in sides:
            ordinal += 1
            game = connection.execute(
                'INSERT INTO games (duel, ordinal, bots, map) VALUES (?, ?, ?, ?)',
                (duel, ordinal, bots, map_payload),
            ).lastrowid
            insert_attempt(connection, game, 1)
    return duel


def _validate_duel_id(duel_id):
    if not isinstance(duel_id, str) or not DUEL_ID.fullmatch(duel_id):
        raise ContestError(400, 'INVALID_DUEL', '对决编号或赛段无效')


def require_duel(connection, duel_id, *, public=False, manual=False):
    _validate_duel_id(duel_id)
    duel = connection.execute('SELECT * FROM duels WHERE duel_id = ?', (duel_id,)).fetchone()
    if duel is not None and duel['kind'] not in DUEL_KINDS:
        duel = None
    if duel is not None:
        is_manual = duel['kind'] == MANUAL_KIND
        if public and (is_manual or duel['batch_id'] not in (None, OFFICIAL_BATCH)):
            duel = None
        elif manual and not is_manual:
            duel = None
    if duel is None:
        raise ContestError(404, 'DUEL_NOT_FOUND', '对决不存在')
    return duel


class EffectiveGame(NamedTuple):
    ordinal: int
    run_id: str
    manifest: dict
    ruling: Optional[dict] = None

    @classmethod
    def from_row(cls, row):
        return cls(row['ordinal'], row['run_id'], json.loads(row['manifest']),
                   json.loads(row['ruling']) if row['ruling'] is not None else None)

    @property
    def teams(self):
        return tuple(bot['team_id'] for bot in self.manifest['bots'])

    @property
    def winner(self):
        if self.ruling is not None:
            return self.ruling['winner']
        seat = self.manifest['result']['winner']
        return None if seat is None else self.teams[seat]

    def summary(self):
        result = {'ordinal': self.ordinal, 'run_id': self.run_id, 'winner': self.winner,
                  'score': dict(zip(self.teams, self.manifest['result']['score']))}
        if self.ruling is not None:
            result['reason'] = self.ruling['reason']
        elif self.manifest['result'].get('reason') == 'double_loss':
            result['reason'] = 'double_loss'
        return result


def _reviewed_attempts(connection, duel):
    return connection.execute('''
        SELECT g.ordinal, r.run_id, r.review, r.manifest, r.ruling
        FROM games g JOIN current_runs r ON r.game = g.id WHERE g.duel = ? ORDER BY g.ordinal
    ''', (duel,))


def _game_results(connection, duel, run_ids):
    effective_ids = set(run_ids)
    return [EffectiveGame.from_row(row).summary() for row in _reviewed_attempts(connection, duel)
            if row['review'] == 'effective' and row['run_id'] in effective_ids]


def _previous_duel_ids(connection, duel, sides):
    """Return each side's closest earlier settlement in the same ladder."""
    remaining = set(sides)
    previous_ids = []
    for previous in connection.execute('''
        SELECT duel_id, submission FROM duels
        WHERE kind = ? AND confirmed_order < ? AND result IS NOT NULL ORDER BY confirmed_order DESC
    ''', (duel['kind'], duel['confirmed_order'])):
        previous_sides = json.loads(previous['submission'])['sides']
        shared = remaining.intersection(previous_sides)
        if shared:
            previous_ids.append(previous['duel_id'])
            remaining.difference_update(shared)
            if not remaining:
                break
    return previous_ids[::-1]


def _manual_summary(row):
    body = json.loads(row['submission'])
    return {'duel_id': row['duel_id'], 'rules': body['rules'], 'sides': body['sides'],
            'accepted_at': row['accepted_at']}


class DuelService:
    def __init__(self, database, phase):
        self.database, self.phase = database, phase

    def accept(self, body):
        sides = _sides(body)
        with self.database.transaction() as connection:
            require_phase(connection, self.phase, LADDER_PHASE)
            return self._schedule_ladder(connection, LADDER_PHASE, sides, 'duel')

    def accept_manual(self, body):
        if not isinstance(body, dict) or body.keys() != {'sides', 'rules'} or body['rules'] != RULESET:
            raise ContestError(400, 'INVALID_DUEL', '请填写双方编号及当前规则（prelim）')
        sides = _sides({'sides': body['sides']}, allow_same=True, first_baseline=True)
        with self.database.transaction() as connection:
            require_phase_available(connection, MANUAL_KIND)
            return self._schedule_ladder(connection, MANUAL_KIND, sides, 'duel-manual')

    def _schedule_ladder(self, connection, kind, sides, prefix):
        duel_id = f'{prefix}-{uuid4().hex}'
        plan = {'duel_id': duel_id, 'rules': RULESET, 'sides': sides}
        maps = [{'seed': secrets.randbelow(999999) + 1}]
        first_bots = sides[::-1] if secrets.randbelow(2) else sides
        insert_plan(connection, kind, plan, maps, first_bots=first_bots)
        return {'duel_id': duel_id}

    def manual_duels(self):
        with self.database.transaction(write=False) as connection:
            rows = connection.execute('''
                SELECT duel_id, submission, accepted_at FROM duels
                WHERE kind = ? ORDER BY id DESC
            ''', (MANUAL_KIND,))
            return {'duels': [_manual_summary(row) for row in rows]}

    def ladder_submissions(self, sides, since):
        sides = _sides({'sides': sides})
        try:
            when = datetime.fromisoformat(since)
            if when.utcoffset() is None:
                raise ValueError('timezone missing')
            cutoff = when.astimezone(ZONE).isoformat(timespec='microseconds')
        except (ValueError, OverflowError) as error:
            raise ValueError('--since 须为包含时区的 ISO 8601 时间') from error
        with self.database.transaction(write=False) as connection:
            rows = connection.execute("""
                SELECT duel_id, submission, accepted_at FROM duels
                WHERE kind = ? AND batch_id IS NULL AND accepted_at >= ?
                ORDER BY accepted_at, id
            """, (LADDER_PHASE, cutoff))
            return {'duels': [
                {'duel_id': row['duel_id'], 'accepted_at': row['accepted_at'], 'sides': sides}
                for row in rows if json.loads(row['submission'])['sides'] == sides
            ]}

    def status(self, duel_id):
        with self.database.transaction(write=False) as connection:
            duel = require_duel(connection, duel_id, public=True)
            return {'duel_id': duel_id, 'state': 'in_progress' if duel['result'] is None else 'finished'}

    def result(self, duel_id):
        with self.database.transaction(write=False) as connection:
            duel = require_duel(connection, duel_id, public=True)
            sides = json.loads(duel['submission'])['sides']
            if duel['result'] is None:
                return {'duel_id': duel_id, 'side_results': dict.fromkeys(sides, 'pending'), 'result': None}
            result = json.loads(duel['result'])
            side_results = {side: 'win' if side == result['winner'] else 'loss' for side in sides}
            result['games'] = _game_results(connection, duel['id'], result['runs'])
            if duel['kind'].endswith('-ladder'):
                result['previous_duel_ids'] = _previous_duel_ids(connection, duel, sides)
            return {'duel_id': duel_id, 'side_results': side_results, 'result': result}

    def show(self, duel_id, *, manual=False):
        with self.database.transaction(write=False) as connection:
            duel = require_duel(connection, duel_id, manual=manual)
            settlement = json.loads(duel['result']) if duel['result'] is not None else None
            result_runs = set()
            if manual and settlement is not None:
                result_runs.update(settlement.get('runs', ()))
                result_runs.add(settlement.get('build_run_id'))
            attempts = []
            for row in connection.execute('''
                SELECT g.ordinal, r.run_id, r.submission, r.arena_result, r.review, r.issue, r.ruling
                FROM games g JOIN runs r ON r.game = g.id
                WHERE g.duel = ? ORDER BY g.ordinal, r.attempt
            ''', (duel['id'],)):
                record = dict(row)
                for field in ('arena_result', 'ruling'):
                    if record[field] is not None:
                        record[field] = json.loads(record[field])
                if manual:
                    review = record['review']
                    arena_state = (record['arena_result'] or {}).get('state')
                    record['has_replay'] = review in REPLAY_REVIEWS
                    record['status'] = review or arena_state or record['submission']
                    if settlement is not None and review is None and record['run_id'] not in result_runs:
                        record['status'] = 'waived'
                attempts.append(record)
            result = {'duel_id': duel_id, 'batch_id': duel['batch_id'], 'attempts': attempts,
                      'issue': DUEL_TIE_ISSUE if duel['awaiting_ruling'] else None,
                      'ruling': json.loads(duel['ruling']) if duel['ruling'] is not None else None,
                      'result': settlement}
            if manual:
                result.update(_manual_summary(duel))
                result['state'] = ('finished' if result['result'] is not None else
                                   'stopped' if duel['closed_at'] is not None else 'in_progress')
            return result

    def batch_replays(self, batch_id, page):
        if batch_id != OFFICIAL_BATCH and not LOAD_TEST_ID.fullmatch(batch_id):
            raise ContestError(400, 'INVALID_LOAD_TEST', '批次编号无效')
        size = BATCH_REPLAY_PAGE_SIZE
        with self.database.transaction(write=False) as connection:
            records = connection.execute('''
                SELECT d.duel_id, g.ordinal, g.bots, r.run_id, r.review
                FROM duels d JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
                WHERE d.batch_id = ? AND d.kind = ? AND r.review IN (?, ?)
                ORDER BY d.id DESC, g.ordinal LIMIT ? OFFSET ?
            ''', (batch_id, RULESET, *REPLAY_REVIEWS, size + 1, page * size)).fetchall()
        return {'runs': [dict(dict(row), bots=json.loads(row['bots'])) for row in records[:size]],
                'has_next': len(records) > size}

    def require_replay(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ContestError(400, 'INVALID_RUN_ID', '执行编号无效')
        with self.database.transaction(write=False) as connection:
            attempt = connection.execute('''
                SELECT r.review FROM current_runs r JOIN games g ON g.id = r.game
                JOIN duels d ON d.id = g.duel
                WHERE r.run_id = ? AND ((d.batch_id IS NOT NULL AND d.kind = ?) OR d.kind = ?)
            ''', (run_id, RULESET, MANUAL_KIND)).fetchone()
        if attempt is None or attempt['review'] not in REPLAY_REVIEWS:
            raise ContestError(404, 'REPLAY_NOT_FOUND', '该局暂无可播放的回放')


def forfeit_result(sides, scheduled, defeated, runs):
    winner = next((team for team in sides if team not in defeated), None)
    return {'outcome': 'double_loss' if winner is None else 'win', 'winner': winner,
            'games_won': {team: scheduled if team == winner else 0 for team in sides},
            'games_lost': {team: scheduled if team in defeated else 0 for team in sides}, 'runs': runs}


def settle(sides, ladder, games, *, build_failure=None, ruling=None):
    """Return the result or keep the duel open while awaiting games or a ruling."""
    scheduled = LADDER_GAMES if ladder else FORMAL_GAMES
    # A manual duel between two copies of one team counts the two seats separately.
    by_seat = sides[0] == sides[1]
    competitors = (0, 1) if by_seat else sides
    run_ids = [game.run_id for game in games]
    if build_failure is not None:
        run_id, manifest = build_failure
        failed = {seat if by_seat else bot['team_id']
                  for seat, bot in enumerate(manifest['bots']) if bot['state'] == 'failed'}
        return dict(forfeit_result(competitors, scheduled, failed, run_ids), build_run_id=run_id)
    if len(games) != scheduled:
        return None

    won = dict.fromkeys(competitors, 0)
    lost = won.copy()
    totals = {key: [0, 0] for key in TIEBREAK_METRICS}
    for game in games:
        record = game.manifest['result']
        winner = record['winner'] if by_seat and game.ruling is None else game.winner
        double_loss = record.get('reason') == 'double_loss'
        completed = record.get('turns') == PARAMS['max_turns']
        for seat, competitor in enumerate(competitors if by_seat else game.teams):
            if competitor == winner:
                won[competitor] += 1
            elif winner is not None or double_loss:
                lost[competitor] += 1
            side = competitors.index(competitor)
            if completed:
                for key in totals:
                    totals[key][side] += record[key][seat]

    result = {'games_won': won, 'games_lost': lost, 'runs': run_ids}
    a, b = competitors
    if won[a] != won[b]:
        return dict(result, outcome='win', winner=a if won[a] > won[b] else b)
    if lost[a] == lost[b] == scheduled:
        return dict(result, outcome='double_loss', winner=None)
    winner, metric = compare_metrics(totals)
    if winner is not None:
        return dict(result, outcome='win', winner=competitors[winner], tiebreak=metric)
    if ruling is not None:
        return dict(result, outcome='win', winner=ruling['winner'], reason=ruling['reason'])


def save_result(connection, duel, result, *, ruling=None):
    connection.execute('''
        UPDATE duels SET result = ?, awaiting_ruling = 0, closed_at = ?, ruling = ?,
            confirmed_order = (SELECT COALESCE(MAX(confirmed_order), 0) + 1 FROM duels)
        WHERE id = ?
    ''', (encode(result), datetime.now(ZONE).isoformat(timespec='microseconds'),
          encode(ruling) if ruling is not None else None, duel))


def settle_duel(connection, duel_id, *, ruling=None):
    """Settle against the current attempts in the same transaction as review."""
    duel = connection.execute('SELECT * FROM duels WHERE id = ?', (duel_id,)).fetchone()
    ladder = duel['kind'].endswith('-ladder')
    effective, build_failure = [], None
    prior_effective = True
    for row in _reviewed_attempts(connection, duel_id):
        if row['review'] == 'effective':
            effective.append(EffectiveGame.from_row(row))
        else:
            if prior_effective and row['review'] == 'build_failed':
                build_failure = row['run_id'], json.loads(row['manifest'])
            prior_effective = False

    submission = json.loads(duel['submission'])
    result = settle(submission['sides'], ladder, effective, build_failure=build_failure, ruling=ruling)
    if result is None:
        if len(effective) == (LADDER_GAMES if ladder else FORMAL_GAMES):
            connection.execute('UPDATE duels SET awaiting_ruling = 1 WHERE id = ?', (duel_id,))
        return
    if ladder:
        row = connection.execute('''
            SELECT result FROM duels WHERE kind = ? AND result IS NOT NULL
            ORDER BY confirmed_order DESC LIMIT 1
        ''', (duel['kind'],)).fetchone()
        ended = datetime.now(ZONE)
        if row is not None:
            latest = datetime.fromisoformat(json.loads(row['result'])['ended_at'])
            ended = max(ended, latest + timedelta(microseconds=1))
        result.update(started_at=duel['started_at'], ended_at=ended.isoformat(timespec='microseconds'))
    save_result(connection, duel_id, result, ruling=ruling)


def _require_run(connection, run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ContestError(400, 'INVALID_RUN_ID', '执行编号无效')
    run = connection.execute(f'''
        SELECT r.review, r.ruling, g.duel, g.bots, d.closed_at, c.run_id AS current_run
        FROM runs r JOIN games g ON g.id = r.game JOIN duels d ON d.id = g.duel
        JOIN current_runs c ON c.game = g.id WHERE r.run_id = ? AND {ACTIVE_DUELS}
    ''', (run_id,)).fetchone()
    if run is None:
        raise ContestError(404, 'RUN_NOT_FOUND', '执行不存在')
    return run


def _reason(value):
    reason = value.strip() if isinstance(value, str) else ''
    if not reason:
        raise ContestError(400, 'INVALID_RULING', '请填写裁定理由')
    return reason


def _existing_ruling(item, **fields):
    if item['ruling'] is not None:
        existing = json.loads(item['ruling'])
        if all(existing.get(key) == value for key, value in fields.items()):
            return existing
        raise ContestError(409, 'RULING_CONFLICT', '该场次已有不同的裁定')
    return None


def _decision(scope, target, reason, operator, **fields):
    return {'scope': scope, 'target': target, 'reason': reason, 'operator': operator,
            'decided_at': datetime.now(ZONE).isoformat(timespec='microseconds'), **fields}


class Rulings:
    def __init__(self, database):
        self.database = database

    def pending(self):
        with self.database.transaction(write=False) as connection:
            rows = connection.execute(f'''
                SELECT d.id AS position, 0 AS ordinal, d.duel_id, d.kind, d.batch_id, d.submission,
                       NULL AS run_id, ? AS issue
                FROM duels d WHERE d.closed_at IS NULL AND {ACTIVE_DUELS} AND d.awaiting_ruling = 1
                UNION ALL
                SELECT d.id, g.ordinal, d.duel_id, d.kind, d.batch_id, d.submission, r.run_id, r.issue
                FROM duels d JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
                WHERE d.closed_at IS NULL AND {ACTIVE_DUELS} AND r.review = 'awaiting_ruling'
                ORDER BY position, ordinal
            ''', (DUEL_TIE_ISSUE,))
            return {'pending': [
                {'scope': 'game' if row['run_id'] else 'duel', 'duel_id': row['duel_id'],
                 'run_id': row['run_id'], 'ordinal': row['ordinal'], 'kind': row['kind'],
                 'batch_id': row['batch_id'], 'sides': json.loads(row['submission'])['sides'],
                 'issue': row['issue']}
                for row in rows
            ]}

    def record(self, scope, target, winner, reason, *, operator):
        if scope not in ('game', 'duel') or type(winner) not in (str, int):
            raise ContestError(400, 'INVALID_RULING', '请选择裁定范围、胜方并填写理由')
        reason = _reason(reason)
        if not fits_text_cell(DUEL_RULING_LABEL.format(reason) if scope == 'duel' else reason):
            raise ContestError(400, 'INVALID_RULING', '裁定理由须能写入 XLSX 文本单元格')
        with self.database.transaction() as connection:
            if scope == 'game':
                item = _require_run(connection, target)
                sides = json.loads(item['bots'])
                pending = item['review'] == 'awaiting_ruling' and item['current_run'] == target
            else:
                item = require_duel(connection, target)
                sides = json.loads(item['submission'])['sides']
                pending = item['awaiting_ruling']
            if sides[0] == sides[1] and str(winner) in ('0', '1'):
                winner = int(winner)
            elif sides[0] == sides[1] or winner not in sides:
                raise ContestError(400, 'INVALID_RULING_WINNER', '裁定胜方必须是本场的参赛方')
            existing = _existing_ruling(item, scope=scope, winner=winner, reason=reason)
            if existing is not None:
                return existing
            if item['closed_at'] is not None or not pending:
                raise ContestError(409, 'RULING_NOT_PENDING', '该场次当前没有待录入的裁定')
            ruling = _decision(scope, target, reason, operator, winner=winner)
            if scope == 'game':
                connection.execute(
                    "UPDATE runs SET ruling = ?, review = 'effective', issue = NULL WHERE run_id = ?",
                    (encode(ruling), target),
                )
                settle_duel(connection, item['duel'])
            else:
                settle_duel(connection, item['id'], ruling=ruling)
            return ruling
