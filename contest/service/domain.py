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
_LEGACY_TEAM = re.compile(r't([0-9]{3})')
_LEGACY_BASELINES = {'bot:L1': 'bot001', 'bot:L2': 'bot002', 'bot:L3': 'bot003'}


def canonical_id(participant):
    """Resolve identities across numbering schemes for persisted duels."""
    legacy_team = _LEGACY_TEAM.fullmatch(participant)
    if legacy_team:
        return f'team{legacy_team[1]}'
    return _LEGACY_BASELINES.get(participant, participant)


class ContestError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def encode(value):
    """Use one canonical representation for persistence and idempotency checks."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

