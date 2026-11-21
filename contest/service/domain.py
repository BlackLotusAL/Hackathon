"""Shared identifiers, errors and value encoding used by Contest features."""
from datetime import timedelta, timezone
import json
import re


DUEL_ID = re.compile(r'duel-[A-Za-z0-9_-]+')
RUN_ID = re.compile(r'run-[0-9]+\.[1-9][0-9]*')
ZONE = timezone(timedelta(hours=8))
PLATFORM_ERRORS = ('judge_crash', 'wall_timeout', 'container_start_failed', 'slot_unhealthy', 'build_platform_error')
TEAM_ID = re.compile(r'team[0-9]{3}')
BASELINES = ('bot001', 'bot002', 'bot003')
OFFICIAL_BATCH = 'official'
LOAD_TEST_ID = re.compile(r'load-[A-Za-z0-9_-]{1,40}')
GAME_TIE_ISSUE = '单局四项指标全同，待主办方裁定'
DUEL_TIE_ISSUE = '整场四项累计指标全同，待主办方裁定'
DUEL_RULING_LABEL = '人工裁定（{}）'


class ContestError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def encode(value):
    """Use one canonical representation for persistence and idempotency checks."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

