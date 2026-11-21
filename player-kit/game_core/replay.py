"""回放数据：首帧给出完整网格，之后每回合只记录变化的格子。练习网页直接读取这个格式。

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
from .rules import Game, PARAMS


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


def replay_to(m, replay, upto):
    """按回放里记录的命令把对局重放到第 upto 回合结束，返回 (game, frames)。"""
    g = Game(m, stage=replay.get('stage'))
    frames = []
    for f in replay['frames'][:upto]:
        cmds = [c if c else None for c in f['cmds']]
        for s in (0, 1):
            g.time_us[s] += round(f.get('ms', [0, 0])[s] * 1000)
        ev = g.step(cmds)
        frames.append((f['cmds'], ev, f.get('ms')))
    return g, frames


def _cells(game):
    N2 = game.N * game.N
    own = ''.join('.' if game.owner[k] < 0 else str(game.owner[k]) for k in range(N2))
    con = ''.join('.' if game.trail[k] < 0 else str(game.trail[k]) for k in range(N2))
    return own, con


class Recorder(object):
    def __init__(self, game, players):
        self.game = game
        cells = _cells(game)
        self.prev = cells
        m = game.map
        init = {'owner': cells[0], 'contour': cells[1]}
        init.update({'beams': [b.to_dict(s) for s, b in enumerate(game.beams)],
                     'score': [game.score(0), game.score(1)], 'bonus': list(game.bonus)})
        self.data = {
            'format': FORMAT,
            'map': replay_map(m),
            'stage': game.stage, 'players': list(players), 'params': dict(PARAMS),
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
