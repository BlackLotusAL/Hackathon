"""Match state, territory capture, and turn settlement."""
from .movement import move_beams
from .rules import PARAMS, compare_metrics
from .schedule import settle_orders, update_boost


class Beam(object):
    __slots__ = ('x', 'y', 'dir', 'speed', 'alive', 'respawn', 'boost_left')

    def __init__(self, x, y, d):
        self.x, self.y, self.dir = x, y, d
        self.speed, self.alive, self.respawn = 1, True, 0
        self.boost_left = 0

    def copy(self):
        b = Beam(self.x, self.y, self.dir)
        b.speed, b.alive, b.respawn = self.speed, self.alive, self.respawn
        b.boost_left = self.boost_left
        return b

    def to_dict(self, seat):
        return {'seat': seat, 'x': self.x, 'y': self.y, 'dir': self.dir, 'speed': self.speed,
                'alive': self.alive, 'restart_in': 0 if self.alive else self.respawn, 'boost_left': self.boost_left}


class Game(object):
    """一局对战的完整状态。

    owner[k]  : -1 无主，0/1 归属
    trail[k]  : -1 无轮廓，0/1 谁的轮廓
    time_us   : 双方累计响应时间（微秒），由裁判累加，用于单局最后一项指标比较（第 9.1 节）
    """

    def __init__(self, m, stage=None, max_turns=None):
        N = self.N = m['size']
        self.map = m
        self.stage = stage or m.get('stage', 'prelim')
        if self.stage != 'prelim':
            raise ValueError('此引擎用于初赛')
        self.terrain = ''.join(m['terrain'])
        NN = N * N
        self.owner = [-1] * NN
        self.trail = [-1] * NN
        self.base = [-1] * NN
        self.cores = [tuple(c) for c in m['calib']]
        for s in (0, 1):
            cx, cy = self.cores[s]
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    k = (cy + dy) * N + cx + dx
                    self.owner[k] = s
                    self.base[k] = s
        self.orders = [list(r) for r in m.get('orders', [])]
        self.order = None          # {'no', 'rect', 'until'}
        self.order_idx = 0
        self.boost_points = [list(p) for p in m.get('boost_points', [])]
        self.boost = None          # 已出现、可拾取的道具 {'x','y'}
        self.boost_next = None     # 已预告、未出现的道具 {'x','y','at'}
        self.boost_idx = 0
        self.picked = [False, False]
        self.bonus = [0, 0]
        self.lead_turns = [0, 0]
        self.beams = [Beam(self.cores[0][0], self.cores[0][1], 'U'),
                      Beam(self.cores[1][0], self.cores[1][1], 'D')]
        self.turn = 0
        self.max_turns = max_turns or PARAMS['max_turns']
        self.done = False
        self.winner = None         # 0 / 1；未结束或指标全同为 None
        self.reason = ''
        self.time_us = [0, 0]
        self.total = sum(PARAMS['key_value'] if c == '*' else 1 for c in self.terrain if c != '#')
        self.events = []

    # ------------------------------------------------------------ 基本工具
    def idx(self, x, y):
        return y * self.N + x

    def inb(self, x, y):
        return 0 <= x < self.N and 0 <= y < self.N

    def val(self, k):
        return PARAMS['key_value'] if self.terrain[k] == '*' else 1

    def score(self, s):
        return sum(self.val(k) for k, owner in enumerate(self.owner) if owner == s) + self.bonus[s]

    def copy(self):
        g = Game.__new__(Game)
        g.__dict__.update(self.__dict__)
        g.owner = self.owner[:]
        g.trail = self.trail[:]
        g.bonus = self.bonus[:]
        g.lead_turns = self.lead_turns[:]
        g.time_us = self.time_us[:]
        g.beams = [b.copy() for b in self.beams]
        g.order = dict(self.order) if self.order else None
        g.boost = dict(self.boost) if self.boost else None
        g.boost_next = dict(self.boost_next) if self.boost_next else None
        g.picked = [False, False]
        g.events = []
        return g

    # ------------------------------------------------------------ 结算子过程
    def fill_cells(self, s):
        """s 在当前局面下闭合会得到的格：自己的全部轮廓格 + 被围住的格（不含基板缺陷和敌方校准区）。"""
        N = self.N
        NN = N * N
        owner, trail, terrain, base = self.owner, self.trail, self.terrain, self.base
        wall = [owner[k] == s or trail[k] == s for k in range(NN)]
        seen = [False] * NN
        st = []
        for k in range(NN):
            x, y = k % N, k // N
            if (x == 0 or y == 0 or x == N - 1 or y == N - 1) and not wall[k]:
                seen[k] = True
                st.append(k)
        while st:
            c = st.pop()
            cx, cy = c % N, c // N
            if cx > 0:
                nk = c - 1
                if not seen[nk] and not wall[nk]:
                    seen[nk] = True; st.append(nk)
            if cx < N - 1:
                nk = c + 1
                if not seen[nk] and not wall[nk]:
                    seen[nk] = True; st.append(nk)
            if cy > 0:
                nk = c - N
                if not seen[nk] and not wall[nk]:
                    seen[nk] = True; st.append(nk)
            if cy < N - 1:
                nk = c + N
                if not seen[nk] and not wall[nk]:
                    seen[nk] = True; st.append(nk)
        out = []
        for k in range(NN):
            if trail[k] == s:
                out.append(k)
            elif not wall[k] and not seen[k] and terrain[k] != '#' and base[k] != 1 - s:
                out.append(k)
        return out

    def step(self, cmds):
        """结算一回合。cmds 为两方命令，例如 ['U', 'L+']；无效或缺失（None）按原方向前进 1 格。
        '+' 只在加速状态下有效（走 2 格），否则被忽略。"""
        if self.done:
            return []
        ev = []
        self.turn += 1
        self.picked = [False, False]
        move_beams(self, cmds, ev)
        # 第 8 步 订单
        if self.orders:
            settle_orders(self, ev)
        # 第 9 步 道具
        if self.boost_points:
            update_boost(self, ev)
        scores = [self.score(s) for s in (0, 1)]
        if scores[0] != scores[1]:
            self.lead_turns[int(scores[1] > scores[0])] += 1
        # 胜负（第 9 节）
        if self.turn >= self.max_turns:
            self.done = True
            self.winner, self.reason = self.decide()
            ev.append({'t': 'end', 's': self.winner, 'reason': self.reason})
        self.events = ev
        return ev

    def metrics(self):
        return {'score': [self.score(0), self.score(1)], 'bonus': list(self.bonus),
                'lead_turns': list(self.lead_turns), 'time_us': list(self.time_us)}

    def decide(self):
        """按规则第 9.1 节判胜，返回胜方出生点编号和胜负原因。"""
        winner, metric = compare_metrics(self.metrics())
        reason = {'score': 'limit', 'time_us': 'time', None: 'tie'}.get(metric, metric)
        return winner, reason

    def forfeit(self, loser, reason):
        """loser 为 -1 时按第 9 节的同分规则判定胜方。"""
        self.done = True
        self.reason = reason
        self.winner = self.decide()[0] if loser == -1 else 1 - loser
        self.events = [{'t': 'end', 's': self.winner, 'reason': reason}]
