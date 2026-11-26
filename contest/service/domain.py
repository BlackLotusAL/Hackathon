"""Shared Contest identifiers, scheduling rules and errors."""
from datetime import timedelta, timezone
import json
import re

from engine.rules import RULESET


DUEL_ID = re.compile(r'duel-[A-Za-z0-9_-]+')
ZONE = timezone(timedelta(hours=8))
TEAM_ID = re.compile(r'team[0-9]{3}')
BASELINES = ('bot001', 'bot002', 'bot003')
OFFICIAL_BATCH = 'official'
LOAD_TEST_ID = re.compile(r'load-[A-Za-z0-9_-]{1,40}')
GAME_TIE_ISSUE = '单局决胜指标全同，待主办方裁定'
DUEL_TIE_ISSUE = '整场无可比较的单局或累计指标全同，待主办方裁定'
DUEL_RULING_LABEL = '人工裁定（{}）'

LADDER_PHASE = 'prelim-ladder'
MANUAL_KIND = 'manual-prelim-ladder'
PHASES = (LADDER_PHASE, RULESET)
DUEL_KINDS = (*PHASES, MANUAL_KIND)
ACTIVE_DUELS = f"d.kind IN ({', '.join(repr(kind) for kind in DUEL_KINDS)})"
_LOAD_TEST_PREFIX = 'load-test-'
DUEL_PHASE = f"""CASE
    WHEN d.batch_id IS NOT NULL AND d.batch_id != '{OFFICIAL_BATCH}' THEN '{_LOAD_TEST_PREFIX}' || d.kind
    ELSE d.kind END"""


class ContestError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def encode(value):
    """Use one canonical representation for persistence and idempotency checks."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def require_phase(connection, configured, *allowed, load_test=False):
    if configured not in allowed:
        raise ContestError(409, 'PHASE_INACTIVE', '当前配置的赛程不受理此操作')
    require_phase_available(connection, configured, load_test=load_test)


def require_phase_available(connection, kind, *, load_test=False):
    requested = _LOAD_TEST_PREFIX + kind if load_test else kind
    previous = connection.execute(f'''
        SELECT {DUEL_PHASE} AS phase FROM duels d
        WHERE d.closed_at IS NULL AND {ACTIVE_DUELS} AND ({DUEL_PHASE}) != ?
        ORDER BY d.id LIMIT 1
    ''', (requested,)).fetchone()
    if previous is not None:
        phase = previous['phase']
        if phase.startswith(_LOAD_TEST_PREFIX):
            label = '批量对战'
        elif phase == MANUAL_KIND:
            label = '单场模拟'
        else:
            label = f'赛程 {phase}'
        raise ContestError(409, 'PHASE_BUSY', f'{label} 仍有未结束的对决')
