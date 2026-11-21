"""Recompute a complete replay from commands and rule observations."""
import json
import re

from . import mapgen
from .adjudication import REQUEST_STATES, STARTUP_STATES, TIME_LIMIT_STATES, apply_startup, apply_turn
from .replay import FORMAT, game_result, public_observation, replay_map
from .rules import ENGINE_VERSION, Game, PARAMS


class VerificationError(ValueError):
    pass


ENGINES = {ENGINE_VERSION: Game}
COMMAND = re.compile(r'[UDLR]\+?')


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def check_fields(recorded, computed, source):
    for key, value in computed.items():
        if key not in recorded or _json(recorded[key]) != _json(value):
            raise VerificationError(f'{source} 的 {key} 与重算不符')


def _pair(value, key):
    if not isinstance(value, list) or len(value) != 2:
        raise VerificationError(f'{key} 必须按出生点提供两项')
    return value


def _exits(observation):
    flags = _pair(observation.get('exits', [False, False]), 'exits')
    if any(type(flag) is not bool for flag in flags):
        raise VerificationError('回放观测的 exits 必须为布尔值')
    return flags


def _observations(replay, log):
    recorded = replay.get('observations')
    if log is not None:
        observed = []
        for entry in log:
            for info in _pair(entry.get('exits', [None, None]), 'exits'):
                if info is not None and (not isinstance(info, dict) or type(info.get('exit_code')) is not int
                                         or type(info.get('oom')) is not bool):
                    raise VerificationError('裁判观测的退出信息无效')
            public = public_observation(entry)
            observed.append(dict(public, exits=_exits(public)))
        if 'observations' in replay:
            projected = [dict(public_observation(entry), exits=_exits(entry)) for entry in recorded]
            if _json(projected) != _json(observed):
                raise VerificationError('回放观测与裁判日志不符')
        recorded = observed
    if not isinstance(recorded, list) or len(recorded) < 2:
        raise VerificationError('回放缺少完整的裁判观测')
    if recorded[0].get('phase') != 'startup' or recorded[-1].get('phase') != 'end':
        raise VerificationError('裁判观测必须按开局、回合、终止排列')
    return recorded


def _recompute(replay, game, observed, log):
    opening, end = observed[0], observed[-1]
    startup = _pair(opening['startup'], 'startup')
    initial = _pair(opening['init'], 'init')
    initial_states = REQUEST_STATES if startup == ['ready', 'ready'] else (None,)
    if any(s not in STARTUP_STATES for s in startup) or any(s not in initial_states for s in initial):
        raise VerificationError('开局请求状态无效')
    errors = [0, 0]
    apply_startup(game, errors, startup, initial, _exits(opening))

    frames = replay['frames']
    if not isinstance(frames, list):
        raise VerificationError('frames 必须是数组')
    limit = PARAMS['turn_ms'] * 1000
    for turn, observation in enumerate(observed[1:-1], 1):
        if game.done or observation.get('phase') is not None:
            raise VerificationError(f'第 {turn} 回合的观测不在有效对局内')
        check_fields(observation, {'turn': turn}, '裁判观测')
        statuses = _pair(observation['status'], 'status')
        us = _pair(observation['us'], 'us')
        for status, elapsed in zip(statuses, us):
            if status not in REQUEST_STATES or type(elapsed) is not int or not 0 <= elapsed <= limit:
                raise VerificationError(f'第 {turn} 回合的请求状态或耗时无效')
            if (status in TIME_LIMIT_STATES and elapsed != limit) or (status == 'crash' and elapsed != 0):
                raise VerificationError(f'第 {turn} 回合的计时不符合请求状态')
        apply_turn(game, errors, statuses, us, _exits(observation))
        if game.done:
            continue
        if turn > len(frames):
            raise VerificationError(f'缺少第 {turn} 回合的回放命令')
        frame = frames[turn - 1]
        check_fields(frame, {'turn': turn}, '回放帧')
        cmds = _pair(frame['cmds'], 'cmds')
        for status, cmd in zip(statuses, cmds):
            valid = isinstance(cmd, str) and COMMAND.fullmatch(cmd) if status == 'ok' else cmd == ''
            if not valid:
                raise VerificationError(f'第 {turn} 回合的命令与请求状态不符')
        if log is not None and 'cmds' in log[turn] and [cmd or '' for cmd in log[turn]['cmds']] != cmds:
            raise VerificationError(f'第 {turn} 回合的命令与裁判日志不符')
        game.step([cmd or None for cmd in cmds])

    if not game.done or game.turn != len(frames):
        raise VerificationError('回放帧和裁判观测未对应到一个完整终局')
    _exits(end)
    check_fields(end, {'reason': game.reason}, '终止观测')
    check_fields(replay['result'], game_result(game), '回放结果')


def verify_replay(replay, *, engine_version=None, board=None, log=None, require_public=False):
    """Verify and return the final game state. A referee log can supply observations."""
    try:
        if replay['format'] != FORMAT or replay['stage'] != 'prelim':
            raise VerificationError('回放格式或赛段无效')
        published = replay.get('engine_version')
        version = published if engine_version is None else engine_version
        if version not in ENGINES:
            raise VerificationError(f'无可用的同版引擎 {version}')
        if 'engine_version' in replay and published != version:
            raise VerificationError('回放与归档结果的引擎版本不符')
        if require_public and (published is None or not isinstance(replay.get('observations'), list)):
            raise VerificationError('正式回放缺少引擎版本或观测')
        check_fields(replay['params'], PARAMS, '初赛回放参数')
        if board is None:
            board = replay['map']
            if mapgen.validate(board) or board.get('stage') != replay['stage']:
                raise VerificationError('回放地图无效')
        else:
            check_fields(replay, {'map': replay_map(board), 'stage': board['stage']}, '回放')
        game = ENGINES[version](board, stage=replay['stage'])
        _recompute(replay, game, _observations(replay, log), log)
        return game
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise VerificationError(f'回放或观测格式无效 ({error})') from error
