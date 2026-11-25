"""Match state, territory capture, and turn settlement."""
from .board import GameMap
from .movement import move_beams
from .rules import PARAMS, RULESET, compare_metrics
from . import prelim


class Beam(object):
    __slots__ = ('x', 'y', 'dir', 'speed', 'alive', 'respawn')

    def __init__(self, x, y, d):
        self.x, self.y, self.dir = x, y, d
        self.speed, self.alive, self.respawn = 1, True, 0

    def copy(self):
        b = Beam(self.x, self.y, self.dir)
        b.speed, b.alive, b.respawn = self.speed, self.alive, self.respawn
        return b

    def to_dict(self, seat):
        return {'seat': seat, 'x': self.x, 'y': self.y, 'dir': self.dir, 'speed': self.speed,
                'alive': self.alive, 'restart_in': 0 if self.alive else self.respawn}


class Game(object):
    """一局对战的完整状态。

    owner[k]  : -1 无主，0/1 归属
    trail[k]  : -1 无轮廓，0/1 谁的轮廓
    time_us   : 双方累计响应时间（微秒），由裁判累加，用于单局最后一项指标比较
    """

    def __init__(self, board, stage=None, max_turns=None):
        self.map = board if isinstance(board, GameMap) else GameMap.from_payload(board)
        if (stage is not None and stage != RULESET) or self.map.stage != RULESET:
            raise ValueError('地图须使用当前赛题规则')
        self.owner = list(self.map.base)
        self.trail = [-1] * len(self.owner)
        self.lead_turns = [0, 0]
        self.beams = [Beam(*center, direction) for center, direction in zip(self.map.calib, ('U', 'D'))]
        self.turn = 0
        self.max_turns = max_turns or PARAMS['max_turns']
        self.done = False
        self.winner = None         # 0 / 1；未结束、双败或指标全同为 None
        self.reason = ''
        self.time_us = [0, 0]
        self.events = []

    # ------------------------------------------------------------ 基本工具
    def idx(self, x, y):
        return y * self.map.size + x

    def inb(self, x, y):
        size = self.map.size
        return 0 <= x < size and 0 <= y < size

    def val(self, k):
        return PARAMS['key_value'] if self.map.tiles[k] == '*' else 1

    def score(self, s):
        return prelim.score(self, s)

    def copy(self):
        g = Game.__new__(Game)
        g.__dict__.update(self.__dict__)
        g.owner = self.owner[:]
        g.trail = self.trail[:]
        g.lead_turns = self.lead_turns[:]
        g.time_us = self.time_us[:]
        g.beams = [b.copy() for b in self.beams]
        g.events = []
        return g

    # ------------------------------------------------------------ 结算子过程
    def fill_cells(self, s):
        """s 在当前局面下闭合会得到的格：自己的全部轮廓格 + 被围住的格（不含基板缺陷和敌方校准区）。"""
        N = self.map.size
        NN = N * N
        owner, trail, terrain, base = self.owner, self.trail, self.map.tiles, self.map.base
        wall = [owner[k] == s or trail[k] == s for k in range(NN)]
        seen = [False] * NN
        st = []
        for k in range(NN):
            x, y = k % N, k // N
            if (x == 0 or y == 0 or x == N - 1 or y == N - 1) and not wall[k]:
                seen[k] = True
                st.append(k)
        adjacent = prelim.neighbors(N)
        while st:
            for nk in adjacent[st.pop()]:
                if not seen[nk] and not wall[nk]:
                    seen[nk] = True
                    st.append(nk)
        return [k for k in range(NN) if owner[k] != s and base[k] < 0 and terrain[k] != '#'
                and (trail[k] == s or not seen[k])]

    def step(self, cmds):
        """按起步格决定移动步数，执行一回合。"""
        if self.done:
            return []
        ev = []
        self.turn += 1
        move_beams(self, cmds, ev)
        scores = [self.score(s) for s in (0, 1)]
        if scores[0] != scores[1]:
            self.lead_turns[int(scores[1] > scores[0])] += 1
        # 胜负
        if self.turn >= self.max_turns:
            self.done = True
            self.winner, self.reason = self.decide()
            ev.append({'t': 'end', 's': self.winner, 'reason': self.reason})
        self.events = ev
        return ev

    def metrics(self):
        return {'score': [self.score(0), self.score(1)],
                'lead_turns': list(self.lead_turns), 'time_us': list(self.time_us)}

    def decide(self):
        """返回胜方出生点编号和胜负原因。"""
        winner, metric = compare_metrics(self.metrics())
        reason = {'score': 'limit', 'time_us': 'time', None: 'tie'}.get(metric, metric)
        return winner, reason

    def forfeit(self, loser, reason):
        """loser 为 -1 表示本局双败。"""
        self.done = True
        self.reason = 'double_loss' if loser == -1 else reason
        self.winner = None if loser == -1 else 1 - loser
        self.events = [{'t': 'end', 's': self.winner, 'reason': self.reason}]
