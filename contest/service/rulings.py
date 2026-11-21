"""Organizer decisions on ties and on eligibility in the final bracket."""
from datetime import datetime
import json

from .domain import DUEL_RULING_LABEL, DUEL_TIE_ISSUE, RUN_ID, ZONE, ContestError, encode
from .duels import FORMAL_GAMES, effective_run_ids, forfeit_result, require_duel, save_result, settle_duel
from .workbook import fits_text_cell


def _require_run(connection, run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ContestError(400, 'INVALID_RUN_ID', '执行编号无效')
    run = connection.execute('''
        SELECT r.review, r.ruling, g.duel, g.bots, d.closed_at, c.run_id AS current_run
        FROM runs r JOIN games g ON g.id = r.game JOIN duels d ON d.id = g.duel
        JOIN current_runs c ON c.game = g.id WHERE r.run_id = ?
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
            rows = connection.execute('''
                SELECT d.id AS position, 0 AS ordinal, d.duel_id, d.kind, d.batch_id, d.submission,
                       NULL AS run_id, ? AS issue
                FROM duels d WHERE d.closed_at IS NULL AND d.awaiting_ruling = 1
                UNION ALL
                SELECT d.id, g.ordinal, d.duel_id, d.kind, d.batch_id, d.submission, r.run_id, r.issue
                FROM duels d JOIN games g ON g.duel = d.id JOIN current_runs r ON r.game = g.id
                WHERE d.closed_at IS NULL AND r.review = 'awaiting_ruling'
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
        if scope not in ('game', 'duel') or not isinstance(winner, str):
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
            if winner not in sides:
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

    def forfeitable_finals(self):
        with self.database.transaction(write=False) as connection:
            rows = connection.execute('''
                SELECT duel_id, submission FROM duels WHERE kind = 'final' AND closed_at IS NULL ORDER BY id
            ''')
            return {'duels': [{'duel_id': row['duel_id'], 'sides': json.loads(row['submission'])['sides']}
                              for row in rows]}

    def forfeit_final(self, duel_id, defeated, reason, *, operator):
        if (not isinstance(defeated, list) or not 1 <= len(defeated) <= 2
                or any(not isinstance(team, str) for team in defeated) or len(set(defeated)) != len(defeated)):
            raise ContestError(400, 'INVALID_FORFEIT', '请选择一支或两支不同的判负队伍')
        reason = _reason(reason)
        defeated = sorted(defeated)
        with self.database.transaction() as connection:
            duel = require_duel(connection, duel_id)
            if duel['kind'] != 'final':
                raise ContestError(409, 'NOT_FINAL', '资格判负仅适用于正式决赛对决')
            sides = json.loads(duel['submission'])['sides']
            if not set(defeated) <= set(sides):
                raise ContestError(400, 'INVALID_FORFEIT', '判负队伍必须属于本场对决')
            existing = _existing_ruling(duel, scope='forfeit', forfeited=defeated, reason=reason)
            if existing is not None:
                return existing
            if duel['closed_at'] is not None:
                raise ContestError(409, 'DUEL_CLOSED', '该场决赛已结算')
            result = forfeit_result(sides, FORMAL_GAMES, defeated, effective_run_ids(connection, duel['id']))
            result.update(forfeited=defeated, reason=reason)
            ruling = _decision('forfeit', duel_id, reason, operator, forfeited=defeated, winner=result['winner'])
            save_result(connection, duel['id'], result, ruling=ruling)
            return ruling
