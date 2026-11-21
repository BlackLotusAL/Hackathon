"""Recompute formal results and verify the replay and referee evidence."""
import json
import re

from . import VERSION, mapgen
from .core import (Game, PARAMS, REQUEST_STATES, STARTUP_STATES, TIME_LIMIT_STATES,
                   apply_startup, apply_turn)
from .protocol import json_object
from .replay import Recorder, game_result, public_observation, replay_map


class VerificationError(ValueError):
    pass


COMMAND = re.compile(r'[UDLR]\+?')


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def check_fields(recorded, expected, source):
    if not isinstance(recorded, dict):
        raise VerificationError(f'{source} 必须是 JSON 对象')
    for key, value in expected.items():
        if key not in recorded or _json(recorded[key]) != _json(value):
            raise VerificationError(f'{source} 的 {key} 与重算不符')


def read_referee_log(path):
    entries = []
    with path.open('rb') as stream:
        for number, line in enumerate(stream, 1):
            try:
                entries.append(json_object(line))
            except (ValueError, RecursionError) as error:
                raise VerificationError(f'裁判日志第 {number} 行不是有效的 JSON 对象') from error
    return entries


def _pair(value, field):
    if not isinstance(value, list) or len(value) != 2:
        raise VerificationError(f'{field} 必须按出生点提供两项')
    return value


def _exit_flags(observation):
    flags = _pair(observation.get('exits', [False, False]), 'exits')
    if any(type(flag) is not bool for flag in flags):
        raise VerificationError('回放观测的 exits 必须为布尔值')
    return flags


def _observations(replay):
    observed = replay.get('observations')
    if not isinstance(observed, list) or len(observed) < 2 or any(not isinstance(entry, dict) for entry in observed):
        raise VerificationError('回放缺少完整的裁判观测')
    if observed[0].get('phase') != 'startup' or observed[-1].get('phase') != 'end':
        raise VerificationError('裁判观测必须按开局、回合、终止排列')
    return observed


def _check_log_observations(log, observations):
    if len(log) != len(observations):
        raise VerificationError('裁判日志与回放的观测数量不一致')
    for number, (entry, observation) in enumerate(zip(log, observations), 1):
        if not isinstance(entry, dict):
            raise VerificationError(f'裁判日志第 {number} 行必须是 JSON 对象')
        for info in _pair(entry.get('exits', [None, None]), '裁判日志 exits'):
            if info is not None and (not isinstance(info, dict) or type(info.get('exit_code')) is not int
                                     or type(info.get('oom')) is not bool):
                raise VerificationError(f'裁判日志第 {number} 行的退出信息无效')
        public = public_observation(entry)
        check_fields(dict(observation, exits=_exit_flags(observation)),
                     dict(public, exits=_exit_flags(public)), f'第 {number} 条回放观测与裁判日志')


def _check_log_fields(entry, expected, source):
    if entry is not None:
        check_fields(entry, {key: value for key, value in expected.items() if key in entry}, source)


def _open_game(game, errors, opening, log_entry):
    startup = _pair(opening['startup'], 'startup')
    initial = _pair(opening['init'], 'init')
    allowed_init = REQUEST_STATES if startup == ['ready', 'ready'] else (None,)
    if any(state not in STARTUP_STATES for state in startup) or any(state not in allowed_init for state in initial):
        raise VerificationError('开局请求状态无效')
    failures = apply_startup(game, errors, startup, initial, _exit_flags(opening))
    expected = {'turn': game.turn, 'errors': errors, 'failures': failures}
    if game.done:
        expected.update(forfeit=game.winner, reason=game.reason)
    _check_log_fields(log_entry, expected, '开局裁判日志')


def _turn_observation(observation, turn):
    source = f'第 {turn} 回合的裁判观测'
    if observation.get('phase') is not None:
        raise VerificationError(f'{source} 的阶段无效')
    check_fields(observation, {'turn': turn}, source)
    statuses = _pair(observation['status'], 'status')
    timing = _pair(observation['us'], 'us')
    limit = PARAMS['turn_ms'] * 1000
    for status, elapsed in zip(statuses, timing):
        if status not in REQUEST_STATES or type(elapsed) is not int or not 0 <= elapsed <= limit:
            raise VerificationError(f'{source} 的请求状态或耗时无效')
        if (status in TIME_LIMIT_STATES and elapsed != limit) or (status == 'crash' and elapsed != 0):
            raise VerificationError(f'{source} 的计时不符合请求状态')
    return statuses, timing, _exit_flags(observation)


def _commands(values, statuses, source):
    commands = _pair(values, source + ' cmds')
    for status, command in zip(statuses, commands):
        valid = isinstance(command, str) and COMMAND.fullmatch(command) if status == 'ok' else command == ''
        if not valid:
            raise VerificationError(f'{source} 的命令与请求状态不符')
    return commands


def _logged_commands(entry, statuses, source):
    if entry is None or 'cmds' not in entry:
        return None
    commands = _pair(entry['cmds'], source + ' cmds')
    return _commands(['' if command is None else command for command in commands], statuses, source)


def _recompute(recorder, frames, observations, log):
    game = recorder.game
    errors = [0, 0]
    _open_game(game, errors, observations[0], log[0] if log is not None else None)

    for turn, observation in enumerate(observations[1:-1], 1):
        if game.done:
            raise VerificationError(f'对局已结束，仍有第 {turn} 回合的观测')
        statuses, timing, exits = _turn_observation(observation, turn)
        entry = log[turn] if log is not None else None
        log_source = f'第 {turn} 回合的裁判日志'
        logged = _logged_commands(entry, statuses, log_source)
        apply_turn(game, errors, statuses, timing, exits)
        if game.done:
            _check_log_fields(entry, {'errors': errors, 'forfeit': game.winner, 'reason': game.reason}, log_source)
            continue

        frame_source = f'第 {turn} 回合的回放帧'
        if turn > len(frames) or not isinstance(frames[turn - 1], dict):
            raise VerificationError(f'缺少有效的{frame_source}')
        frame = frames[turn - 1]
        commands = _commands(frame['cmds'], statuses, frame_source)
        if logged is not None and logged != commands:
            raise VerificationError(f'{frame_source} 的命令与裁判日志不符')
        events = game.step([command or None for command in commands])
        expected = recorder.record(commands, events)
        check_fields(frame, expected, frame_source)
        _check_log_fields(entry, {'errors': errors, 'score': expected['score'], 'events': events}, log_source)

    if not game.done or len(frames) != game.turn:
        raise VerificationError('回放帧和裁判观测未对应到一个完整终局')
    end = observations[-1]
    _exit_flags(end)
    check_fields(end, {'reason': game.reason}, '终止观测')
    _check_log_fields(log[-1] if log is not None else None,
                      {'turn': game.turn, 'winner': game.winner}, '终止裁判日志')


def _replay_game(replay, board):
    version = replay.get('version')
    if version != VERSION:
        raise VerificationError(f'回放版本不受支持（{version!r}）')
    if replay.get('source') == 'practice':
        raise VerificationError('正式复核需要裁判回放')
    stage = replay['stage']
    if stage not in ('prelim', 'final'):
        raise VerificationError('回放赛段无效')
    if board is None:
        board = replay['map']
        if (not isinstance(board.get('id'), str) or not board['id']
                or board.get('stage') != stage or mapgen.validate(board)):
            raise VerificationError('回放地图无效')
    else:
        check_fields(replay, {'map': replay_map(board), 'stage': board['stage']}, '回放')
    return Game(board, stage=stage)


def verify_replay(replay, *, board=None, log=None):
    """Check the recorded initial state, all settled rounds, and the result."""
    try:
        game = _replay_game(replay, board)
        players = _pair(replay['players'], 'players')
        if any(not isinstance(name, str) for name in players):
            raise VerificationError('回放中的 Bot 名称须为字符串')
        recorder = Recorder(game, players)
        check_fields(replay, {'params': recorder.data['params']}, '回放')
        check_fields(replay['init'], recorder.data['init'], '回放初始局面')

        frames = replay['frames']
        if not isinstance(frames, list):
            raise VerificationError('frames 必须是数组')
        observations = _observations(replay)
        if log is not None:
            _check_log_observations(log, observations)
        _recompute(recorder, frames, observations, log)
        check_fields(replay['result'], game_result(game), '回放结果')
        return game
    except VerificationError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, RecursionError) as error:
        raise VerificationError(f'回放或观测格式无效 ({error})') from error
