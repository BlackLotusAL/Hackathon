"""Duel planning, settlement and Portal queries."""
from datetime import datetime, timedelta
import json
import secrets

from engine.core import TIEBREAK_METRICS, compare_metrics
from engine.mapgen import load_map
from engine.protocol import json_object

from .domain import BASELINES, DUEL_ID, TEAM_ID, ZONE, ContestError, canonical_id, encode


FORMAL_MAPS = 3
LADDER_GAMES = 2
FORMAL_GAMES = FORMAL_MAPS * 2


def load_pools(path):
    """Load the sealed map files and check their published SHA-256 values."""
    result = {}
    pools = json_object(path.read_bytes()) if path else {}
    for stage, entries in pools.items():
        if stage not in ('prelim', 'final') or not isinstance(entries, list) or len(entries) != FORMAL_MAPS:
            raise ValueError('每个正式地图池必须包含三张地图')
        specs, ids = [], set()
        for entry in entries:
            raw = (path.parent / entry['file']).read_bytes().decode('utf-8')
            spec = {'file': raw, 'sha256': entry['sha256']}
            board, _ = load_map(spec, stage)
            ids.add(board['id'])
            specs.append(spec)
        if len(ids) != len(entries):
            raise ValueError('正式地图编号重复')
        result[stage] = specs
    return result


def formal_maps(pools, stage):
    maps = pools.get(stage)
    if not maps:
        raise ContestError(503, 'MAP_POOL_UNAVAILABLE', '尚未配置该赛段的正式地图池')
    return maps


def _duel_kind(body):
    rules, sides = body.get('rules'), body.get('sides')
    if rules not in ('prelim', 'final'):
        raise ContestError(400, 'INVALID_DUEL', '对决编号或赛段无效')
    if (not isinstance(sides, list) or len(sides) != 2 or sides[0] == sides[1]
            or any(not isinstance(team, str) or (not TEAM_ID.fullmatch(team) and team not in BASELINES) for team in sides)):
        raise ContestError(400, 'INVALID_DUEL', '双方编号无效')
    selector = body.get('map_seed', rules)
    ladder = type(selector) is int or selector == 'random'
    if (not ladder and selector != rules) or sum(team in BASELINES for team in sides) > int(ladder):
        raise ContestError(400, 'INVALID_DUEL', '地图选择或参赛方不适用于本场')
    return f'{rules}-ladder' if ladder else rules


def insert_attempt(connection, game, attempt):
    run_id = f'run-{game}.{attempt}'
    connection.execute('INSERT INTO runs (run_id, game, attempt) VALUES (?, ?, ?)', (run_id, game, attempt))
    return run_id


def insert_plan(connection, kind, body, maps):
    """Persist the duel, its immutable game plan and the first attempts together."""
    duel = connection.execute(
        'INSERT INTO duels (duel_id, submission, kind) VALUES (?, ?, ?)',
        (body['duel_id'], encode(body), kind),
    ).lastrowid
    sides = (encode(body['sides']), encode(body['sides'][::-1]))
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


def _require_duel(connection, duel_id):
    _validate_duel_id(duel_id)
    duel = connection.execute('SELECT * FROM duels WHERE duel_id = ?', (duel_id,)).fetchone()
    if duel is None:
        raise ContestError(404, 'DUEL_NOT_FOUND', '对决不存在')
    return duel


def _game_results(connection, duel, run_ids):
    if not run_ids:
        return []
    effective_ids = set(run_ids)
    games = []
    for row in connection.execute('''
        SELECT g.ordinal, r.run_id, r.manifest FROM games g JOIN runs r ON r.game = g.id
        WHERE g.duel = ? AND r.review = 'effective' ORDER BY g.ordinal
    ''', (duel,)):
        if row['run_id'] in effective_ids:
            manifest = json.loads(row['manifest'])
            teams = [bot['team_id'] for bot in manifest['bots']]
            result = manifest['result']
            games.append({'ordinal': row['ordinal'], 'run_id': row['run_id'],
                          'winner': teams[result['winner']], 'score': dict(zip(teams, result['score']))})
    return games


def _previous_duel_ids(connection, duel, sides):
    """Return each side's closest earlier settlement in the same ladder."""
    remaining = {canonical_id(side) for side in sides}
    previous_ids = []
    for previous in connection.execute('''
        SELECT duel_id, submission FROM duels
        WHERE kind = ? AND confirmed_order < ? AND result IS NOT NULL ORDER BY confirmed_order DESC
    ''', (duel['kind'], duel['confirmed_order'])):
        previous_sides = json.loads(previous['submission'])['sides']
        shared = remaining.intersection(map(canonical_id, previous_sides))
        if shared:
            previous_ids.append(previous['duel_id'])
            remaining.difference_update(shared)
            if not remaining:
                break
    return previous_ids[::-1]


class DuelService:
    def __init__(self, database, maps):
        self.database, self.maps = database, maps

    def accept(self, body):
        duel_id = body.get('duel_id')
        _validate_duel_id(duel_id)
        try:
            submission = encode(body)
        except ValueError as error:
            raise ContestError(400, 'INVALID_DUEL', '请求含无效 JSON 数值') from error
        with self.database.transaction() as connection:
            existing = connection.execute('SELECT submission FROM duels WHERE duel_id = ?', (duel_id,)).fetchone()
            if existing is not None and existing['submission'] == submission:
                return
            kind = _duel_kind(body)
            if existing is not None:
                raise ContestError(409, 'DUEL_CONFLICT', '同一对决编号的请求内容冲突')
            if kind.endswith('-ladder'):
                seed = body['map_seed']
                maps = [{'seed': secrets.randbelow(999999) + 1 if seed == 'random' else seed}]
            else:
                maps = formal_maps(self.maps, body['rules'])
            insert_plan(connection, kind, body, maps)

    def status(self, duel_id):
        with self.database.transaction(write=False) as connection:
            duel = _require_duel(connection, duel_id)
            return {'duel_id': duel_id, 'state': 'in_progress' if duel['result'] is None else 'finished'}

    def result(self, duel_id):
        with self.database.transaction(write=False) as connection:
            duel = _require_duel(connection, duel_id)
            sides = json.loads(duel['submission'])['sides']
            if duel['result'] is None:
                return {'duel_id': duel_id, 'side_results': dict.fromkeys(sides, 'pending'), 'result': None}
            result = json.loads(duel['result'])
            if result['outcome'] == 'draw':
                side_results = dict.fromkeys(sides, 'draw')
            else:
                side_results = {side: 'win' if side == result['winner'] else 'loss' for side in sides}
            result['games'] = _game_results(connection, duel['id'], result['runs'])
            if duel['kind'].endswith('-ladder'):
                result['previous_duel_ids'] = _previous_duel_ids(connection, duel, sides)
            return {'duel_id': duel_id, 'side_results': side_results, 'result': result}

    def show(self, duel_id):
        with self.database.transaction(write=False) as connection:
            duel = _require_duel(connection, duel_id)
            attempts = []
            for row in connection.execute('''
                SELECT g.ordinal, r.run_id, r.submission, r.arena_result, r.review, r.issue
                FROM games g JOIN runs r ON r.game = g.id
                WHERE g.duel = ? ORDER BY g.ordinal, r.attempt
            ''', (duel['id'],)):
                record = dict(row)
                if record['arena_result'] is not None:
                    record['arena_result'] = json.loads(record['arena_result'])
                attempts.append(record)
            return {'duel_id': duel_id, 'issue': duel['issue'], 'attempts': attempts,
                    'result': json.loads(duel['result']) if duel['result'] is not None else None}


def forfeit_result(sides, scheduled, defeated, runs):
    winner = next((team for team in sides if team not in defeated), None)
    return {'outcome': 'double_loss' if winner is None else 'win', 'winner': winner,
            'games_won': {team: scheduled if team == winner else 0 for team in sides},
            'games_lost': {team: scheduled if team in defeated else 0 for team in sides}, 'runs': runs}


def settle(sides, ladder, runs, *, build_failure=None):
    """Return the result or keep the duel open while awaiting games or a ruling."""
    scheduled = LADDER_GAMES if ladder else FORMAL_GAMES
    run_ids = [run_id for run_id, _ in runs]
    if build_failure is not None:
        run_id, manifest = build_failure
        failed = {bot['team_id'] for bot in manifest['bots'] if bot['state'] == 'failed'}
        return dict(forfeit_result(sides, scheduled, failed, run_ids), build_run_id=run_id)
    if len(runs) != scheduled:
        return None

    won = dict.fromkeys(sides, 0)
    lost = won.copy()
    totals = {key: [0, 0] for key in TIEBREAK_METRICS}
    for _, manifest in runs:
        single = manifest['result']
        if single['winner'] is None:
            return None
        for seat, bot in enumerate(manifest['bots']):
            team = bot['team_id']
            if seat == single['winner']:
                won[team] += 1
            else:
                lost[team] += 1
            side = sides.index(team)
            for key in totals:
                totals[key][side] += single[key][seat]

    result = {'games_won': won, 'games_lost': lost, 'runs': run_ids}
    a, b = sides
    if won[a] != won[b]:
        return dict(result, outcome='win', winner=a if won[a] > won[b] else b)
    if ladder:
        return dict(result, outcome='draw', winner=None)
    winner, metric = compare_metrics(totals)
    if winner is not None:
        return dict(result, outcome='win', winner=sides[winner], tiebreak=metric)
    return None


def save_result(connection, duel, result):
    connection.execute('''
        UPDATE duels SET result = ?, issue = NULL,
            confirmed_order = (SELECT COALESCE(MAX(confirmed_order), 0) + 1 FROM duels)
        WHERE id = ?
    ''', (encode(result), duel))


def settle_duel(connection, duel_id):
    """Settle against the current attempts in the same transaction as review."""
    duel = connection.execute('SELECT * FROM duels WHERE id = ?', (duel_id,)).fetchone()
    ladder = duel['kind'].endswith('-ladder')
    effective, build_failure = [], None
    prior_effective = True
    for row in connection.execute('''
        SELECT r.run_id, r.review, r.manifest FROM current_runs r JOIN games g ON g.id = r.game
        WHERE g.duel = ? ORDER BY g.ordinal
    ''', (duel['id'],)):
        if row['review'] == 'effective':
            effective.append((row['run_id'], json.loads(row['manifest'])))
        else:
            if prior_effective and row['review'] == 'build_failed':
                build_failure = row['run_id'], json.loads(row['manifest'])
            prior_effective = False

    sides = json.loads(duel['submission'])['sides']
    result = settle(sides, ladder, effective, build_failure=build_failure)
    if result is None:
        if len(effective) == (LADDER_GAMES if ladder else FORMAL_GAMES):
            connection.execute('UPDATE duels SET issue = ? WHERE id = ?',
                               ('整场四项累计指标全同，待主办方处理', duel['id']))
        return
    if ladder:
        row = connection.execute('''
            SELECT result FROM duels WHERE kind LIKE '%-ladder' AND result IS NOT NULL
            ORDER BY confirmed_order DESC LIMIT 1
        ''').fetchone()
        ended = datetime.now(ZONE)
        if row is not None:
            latest = datetime.fromisoformat(json.loads(row['result'])['ended_at'])
            ended = max(ended, latest + timedelta(microseconds=1))
        result.update(started_at=duel['started_at'], ended_at=ended.isoformat(timespec='microseconds'))
    save_result(connection, duel['id'], result)
