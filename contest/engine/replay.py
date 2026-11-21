"""回放数据：首帧给出完整网格，之后每回合只记录变化的格子。选手材料包的练习网页直接读取这个格式。

格式（arena-replay/2）：
{
  "format": "arena-replay/2",
  "map": {...地图...}, "stage": "prelim", "players": ["bot003", "bot002"],
  "params": {...规则参数...},
  "init": {"owner": "...", "contour": "...", "beams": [...], "score": [..], "bonus": [..]},
  "frames": [
     {"turn": 1, "cmds": ["U", "D"], "ms": [12, 40], "beams": [...], "score": [..], "bonus": [..], "order": null,
      "boost": null, "boost_next": null, "events": [...], "d": [[格子编号, 归属, 轮廓], ...]}
  ],
  "result": {"winner": 0, "reason": "limit", "score": [..], "bonus": [..], "turns": 600, "lead_turns": [..], "time_us": [..]}
}
格子编号 k = y * size + x；归属/轮廓字符 '.' '0' '1'。ms 为双方本回合的响应时间（毫秒）。
boost 为已出现的加速道具 {x, y}，boost_next 为已预告的道具 {x, y, at}；束斑的 boost_left 为剩余加速回合。
"""
import json
import re

from . import mapgen
from .core import (ENGINE_VERSION, Game, PARAMS, PRELIM_PARAMS, REQUEST_STATES,
                   STARTUP_STATES, TIME_LIMIT_STATES, apply_startup, apply_turn)


FORMAT = 'arena-replay/2'


def replay_map(board):
    keys = ('id', 'name', 'en', 'stage', 'size', 'calib', 'terrain', 'orders', 'boost_points')
    return {key: board[key] for key in keys if key in board}


def game_result(game):
    return {'winner': game.winner, 'reason': game.reason, 'turns': game.turn, **game.metrics()}


def public_observation(entry):
    """Keep the rule inputs and omit private Bot output and exit details."""
    keys = {
        'startup': ('phase', 'startup', 'init'),
        'end': ('phase', 'reason'),
        None: ('turn', 'status', 'us'),
    }[entry.get('phase')]
    result = {key: entry[key] for key in keys}
    if 'exits' in entry:
        result['exits'] = [info if type(info) is bool else info is not None for info in entry['exits']]
    return result


def _cells(game):
    N2 = game.N * game.N
    own = ''.join('.' if game.owner[k] < 0 else str(game.owner[k]) for k in range(N2))
    con = ''.join('.' if game.trail[k] < 0 else str(game.trail[k]) for k in range(N2))
    if game.final:
        lat = ''.join(str(game.regions[game.lat[k]][1]) if game.lat[k] else '.' for k in range(N2))
        return own, con, lat
    return own, con


class Recorder(object):
    def __init__(self, game, players):
        self.game = game
        cells = _cells(game)
        self.prev = cells
        m = game.map
        init = {'owner': cells[0], 'contour': cells[1]}
        if len(cells) > 2:
            init['latent'] = cells[2]
        init.update({'beams': [b.to_dict(s) for s, b in enumerate(game.beams)],
                     'score': [game.score(0), game.score(1)], 'bonus': list(game.bonus)})
        self.data = {
            'format': FORMAT,
            'map': replay_map(m),
            'stage': game.stage, 'players': list(players),
            'params': dict(PRELIM_PARAMS if game.stage == 'prelim' else PARAMS),
            'init': init, 'frames': [], 'result': None,
        }

    def record(self, cmds, events, ms=None):
        g = self.game
        cells = _cells(g)
        prev = self.prev
        n = len(cells)
        d = [[k] + [c[k] for c in cells] for k in range(len(cells[0]))
             if any(cells[i][k] != prev[i][k] for i in range(n))]
        self.prev = cells
        o = g.order
        f = {'turn': g.turn, 'cmds': list(cmds)}
        if ms is not None:
            f['ms'] = list(ms)
        self.data['frames'].append(f)
        f.update({
            'beams': [b.to_dict(s) for s, b in enumerate(g.beams)],
            'score': [g.score(0), g.score(1)], 'bonus': list(g.bonus),
            'order': {'no': o['no'], 'rect': o['rect'], 'until': o['until']} if o else None,
            'boost': dict(g.boost) if g.boost else None,
            'boost_next': dict(g.boost_next) if g.boost_next else None,
            'events': events, 'd': d,
        })

    def finish(self):
        self.data['result'] = game_result(self.game)
        return self.data


class VerificationError(ValueError):
    pass


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
    if not isinstance(recorded, list) or len(recorded) < 2:
        raise VerificationError('回放缺少完整的裁判观测')
    if log is not None:
        observed = []
        for entry in log:
            for info in _pair(entry.get('exits', [None, None]), 'exits'):
                if info is not None and (not isinstance(info, dict) or type(info.get('exit_code')) is not int
                                         or type(info.get('oom')) is not bool):
                    raise VerificationError('裁判观测的退出信息无效')
            public = public_observation(entry)
            observed.append(dict(public, exits=_exits(public)))
        projected = [dict(public_observation(entry), exits=_exits(entry)) for entry in recorded]
        if _json(projected) != _json(observed):
            raise VerificationError('回放观测与裁判日志不符')
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


def verify_replay(replay, *, board=None, log=None):
    """Recompute the public replay and cross-check the referee log when supplied."""
    try:
        if replay['format'] != FORMAT or replay['stage'] not in ('prelim', 'final'):
            raise VerificationError('回放格式或赛段无效')
        version = replay.get('engine_version')
        if version != ENGINE_VERSION:
            raise VerificationError(f'无可用的同版引擎 {version}')
        expected_params = PRELIM_PARAMS if replay['stage'] == 'prelim' else PARAMS
        check_fields(replay, {'params': expected_params}, '回放')
        if board is None:
            board = replay['map']
            if mapgen.validate(board) or board.get('stage') != replay['stage']:
                raise VerificationError('回放地图无效')
        else:
            check_fields(replay, {'map': replay_map(board), 'stage': board['stage']}, '回放')
        game = Game(board, stage=replay['stage'])
        _recompute(replay, game, _observations(replay, log), log)
        return game
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise VerificationError(f'回放或观测格式无效 ({error})') from error
