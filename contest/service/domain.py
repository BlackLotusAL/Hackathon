"""Shared Contest identifiers, scheduling rules and errors."""
from datetime import timedelta, timezone
import json
import re


DUEL_ID = re.compile(r'duel-[A-Za-z0-9_-]+')
ZONE = timezone(timedelta(hours=8))
PLATFORM_ERRORS = ('judge_crash', 'wall_timeout', 'container_start_failed', 'slot_unhealthy', 'build_platform_error')
TEAM_ID = re.compile(r'team[0-9]{3}')
BASELINES = ('bot001', 'bot002', 'bot003')
OFFICIAL_BATCH = 'official'
LOAD_TEST_ID = re.compile(r'load-[A-Za-z0-9_-]{1,40}')
GAME_TIE_ISSUE = '单局四项指标全同，待主办方裁定'
DUEL_TIE_ISSUE = '整场四项累计指标全同，待主办方裁定'
DUEL_RULING_LABEL = '人工裁定（{}）'

PORTAL_DUEL_RULES = {'prelim-ladder': 'prelim', 'final-ladder': 'final', 'final': 'final'}
PHASES = ('prelim-ladder', 'prelim', 'final-ladder', 'final')
_LOAD_TEST_GROUP = 'load-test'
DUEL_PHASE = f"""CASE
    WHEN d.batch_id IS NOT NULL AND d.batch_id != '{OFFICIAL_BATCH}' THEN '{_LOAD_TEST_GROUP}'
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
    # Load tests share the preliminary configuration and run in a separate group.
    requested = _LOAD_TEST_GROUP if load_test else configured
    previous = connection.execute(f'''
        SELECT {DUEL_PHASE} AS phase FROM duels d
        WHERE d.closed_at IS NULL AND ({DUEL_PHASE}) != ?
        ORDER BY d.id LIMIT 1
    ''', (requested,)).fetchone()
    if previous is not None:
        phase = previous['phase']
        label = '压测批次' if phase == _LOAD_TEST_GROUP else f'赛程 {phase}'
        raise ContestError(409, 'PHASE_BUSY', f'{label} 仍有未结束的对决')
