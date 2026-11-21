"""曝光领地 · 规则引擎。

回合步骤对应《赛题规则》，结算只依赖 Python 标准库。
"""

DX = {'U': 0, 'D': 0, 'L': -1, 'R': 1}
DY = {'U': -1, 'D': 1, 'L': 0, 'R': 0}
OPP = {'U': 'D', 'D': 'U', 'L': 'R', 'R': 'L'}
DIRS = ('U', 'D', 'L', 'R')
TIEBREAK_METRICS = ('score', 'bonus', 'lead_turns', 'time_us')


def compare_metrics(metrics):
    """Return the winning side index and the first differing metric."""
    for key in TIEBREAK_METRICS:
        a, b = metrics[key]
        if a != b:
            return int(b < a) if key == 'time_us' else int(b > a), key
    return None, None

# 与规则文档各节的参数一致
PRELIM_PARAMS = {
    'size': 41,               # 基板边长
    'max_turns': 600,         # 回合上限
    'restart': 5,             # 轮廓报废后停机校准回合
    'key_value': 3,           # 关键图形区价值
    'order_first': 40,        # 第一个订单在第 40 回合末公示
    'order_every': 40,        # 之后每 40 回合公示一个
    'order_duration': 30,     # 公示后第 30 个回合末到期结算
    'order_bonus': 1,         # 到期时区域内每个己方图形区格奖励 1 倍价值
    'boost_every': 120,       # 加速道具刷新间隔 N：第 120 回合末起每 120 回合出现一个
    'boost_lead': 30,         # 提前 30 回合预告位置
    'boost_duration': 40,     # 拾取后加速持续 M 回合
    'startup_s': 10,          # 启动：GET /v1/health 须在 10 秒内返回就绪
    'init_ms': 2000,          # 开局请求时限
    'turn_ms': 200,           # 每回合请求时限
    'end_ms': 1000,           # 结束请求时限
    'max_errors': 10,         # 累计错误达到此数判负（规则第 9 节）
}

PARAMS = {
    **PRELIM_PARAMS,
    # 决赛新增（第 10 节）
    'latent': 8,              # 潜像期（回合）
    'decay': 2,               # 每回合失效的不接地格数
}


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
        self.final = self.stage == 'final'
        self.terrain = ''.join(m['terrain'])
        NN = N * N
        self.owner = [-1] * NN
        self.lat = [0] * NN  # 0 为已显影或无主，正值为潜像区域编号
        self.trail = [-1] * NN
        self.base = [-1] * NN
        self.regions = {}  # 区域编号 -> [归属, 回合计时]
        self.next_region = 1
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

    def dev_value(self, s):
        o, l, t = self.owner, self.lat, self.terrain
        kv = PARAMS['key_value']
        return sum((kv if t[k] == '*' else 1) for k in range(len(o)) if o[k] == s and l[k] == 0)

    def lat_value(self, s):
        o, l, t = self.owner, self.lat, self.terrain
        kv = PARAMS['key_value']
        return sum((kv if t[k] == '*' else 1) for k in range(len(o)) if o[k] == s and l[k] > 0)

    def latent_left(self, k):
        """潜像格还要几个回合末显影（1～8）；不是潜像返回 0。"""
        r = self.lat[k]
        return self.regions[r][1] if r else 0

    def score(self, s):
        return self.dev_value(s) + self.bonus[s]

    def copy(self):
        g = Game.__new__(Game)
        g.__dict__.update(self.__dict__)
        g.owner = self.owner[:]
        g.lat = self.lat[:]
        g.trail = self.trail[:]
        g.regions = {k: v[:] for k, v in self.regions.items()}
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
    def _void_region(self, rid):
        n = 0
        lat, owner = self.lat, self.owner
        for k in range(len(lat)):
            if lat[k] == rid:
                lat[k] = 0
                owner[k] = -1
                n += 1
        self.regions.pop(rid, None)
        return n

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

    def _substep(self, movers, cut, ev):
        """一个移动步（第 2～6 步）。movers 为本移动步移动的座位。"""
        N = self.N
        start_trail = self.trail[:]
        tx, ty = [0, 0], [0, 0]
        moving = [False, False]
        # 第 2 步 撞缺陷与出界
        for s in movers:
            b = self.beams[s]
            tx[s], ty[s] = b.x + DX[b.dir], b.y + DY[b.dir]
            moving[s] = True
            if not self.inb(tx[s], ty[s]) or self.terrain[self.idx(tx[s], ty[s])] == '#':
                cut[s] = True
                moving[s] = False
                ev.append({'t': 'crash', 's': s})
        # 第 3 步 束斑碰撞
        if moving[0] and moving[1]:
            a, b = self.beams
            same = tx[0] == tx[1] and ty[0] == ty[1]
            swap = tx[0] == b.x and ty[0] == b.y and tx[1] == a.x and ty[1] == a.y
            if same or swap:
                for s in (0, 1):
                    if self.owner[self.idx(tx[s], ty[s])] != s:
                        cut[s] = True
                        moving[s] = False
                ev.append({'t': 'collide'})
        # 第 4 步 移动
        for s in (0, 1):
            if moving[s]:
                self.beams[s].x, self.beams[s].y = tx[s], ty[s]
        # 第 5 步 扫过轮廓（按本移动步开始时的轮廓判断）
        for s in (0, 1):
            if not moving[s]:
                continue
            k = self.idx(self.beams[s].x, self.beams[s].y)
            tr = start_trail[k]
            if tr == s:
                cut[s] = True
                ev.append({'t': 'selfcut', 's': s})
            elif tr == 1 - s:
                cut[1 - s] = True
                ev.append({'t': 'cut', 's': s})
            if self.owner[k] == 1 - s and self.lat[k] > 0:
                n = self._void_region(self.lat[k])
                ev.append({'t': 'void', 's': s, 'n': n, 'x': self.beams[s].x, 'y': self.beams[s].y})
        # 第 6 步 闭合与延伸
        closers = []
        for s in (0, 1):
            if not moving[s] or cut[s]:
                continue
            k = self.idx(self.beams[s].x, self.beams[s].y)
            if self.owner[k] == s and s in self.trail:
                closers.append(s)
        fills, claim = {}, {}
        for s in closers:   # 双方都按闭合前的局面计算
            fills[s] = self.fill_cells(s)
            for c in fills[s]:
                claim[c] = claim.get(c, 0) | (1 << s)
        both = len(closers) == 2
        for s in closers:
            rid = self.next_region
            self.next_region += 1
            n = 0
            value = 0
            for c in fills[s]:
                if self.trail[c] == s:
                    self.trail[c] = -1
                if self.base[c] >= 0 or self.owner[c] == s:
                    continue
                if both and claim[c] == 3:
                    continue          # 被双方同时围到：保持原归属
                if not both and self.trail[c] == 1 - s:
                    cut[1 - s] = True
                    ev.append({'t': 'enclose', 's': s})
                self.owner[c] = s
                self.lat[c] = rid if self.final else 0     # 决赛：成为潜像；初赛：直接成为图形区
                n += 1
                value += self.val(c)
            for q in range(len(self.trail)):
                if self.trail[q] == s:
                    self.trail[q] = -1
            if n > 0 and self.final:
                self.regions[rid] = [s, PARAMS['latent'] + 1]
            ev.append({'t': 'close', 's': s, 'gain': value})
        for s in (0, 1):
            if not moving[s] or cut[s]:
                continue
            k = self.idx(self.beams[s].x, self.beams[s].y)
            if self.owner[k] != s:
                self.trail[k] = s
        if self.boost:                                     # 拾取道具（闭合结算之后）
            for s in (0, 1):
                b = self.beams[s]
                if moving[s] and not cut[s] and b.x == self.boost['x'] and b.y == self.boost['y']:
                    b.boost_left = PARAMS['boost_duration']
                    self.picked[s] = True
                    ev.append({'t': 'pickup', 's': s, 'x': b.x, 'y': b.y})
                    self.boost = None
                    break
        return moving

    def _ground(self, s):
        """决赛第 9 步：接地。返回本回合失效的格数。"""
        N = self.N
        owner, lat = self.owner, self.lat
        cx, cy = self.cores[s]
        start = cy * N + cx
        seen = {start}
        q = [start]
        h = 0
        while h < len(q):
            c = q[h]; h += 1
            x, y = c % N, c // N
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < N and 0 <= ny < N:
                    nk = ny * N + nx
                    if nk not in seen and owner[nk] == s:   # 已显影和潜像都导电
                        seen.add(nk); q.append(nk)
        loose = [k for k in range(N * N) if owner[k] == s and lat[k] == 0 and k not in seen]
        if not loose:
            return 0
        loose.sort(key=lambda k: (-(abs(k % N - cx) + abs(k // N - cy)), k // N, k % N))
        n = min(PARAMS['decay'], len(loose))
        for k in loose[:n]:
            owner[k] = -1
        return n

    def order_cells(self, rect):
        return [self.idx(x, y) for y in range(rect[1], rect[3] + 1) for x in range(rect[0], rect[2] + 1)
                if self.terrain[self.idx(x, y)] != '#']

    def _order_step(self, ev):
        """客户订单（第 8 步）。先结算到期订单，再公示新订单。"""
        o = self.order
        if o and self.turn >= o['until']:
            got = [0, 0]
            for k in self.order_cells(o['rect']):
                w = self.owner[k]
                if w >= 0 and self.lat[k] == 0:
                    b = self.val(k) * PARAMS['order_bonus']
                    self.bonus[w] += b
                    got[w] += b
            ev.append({'t': 'orderexpire', 'no': o['no'], 'got': got})
            self.order = None
        t0, every = PARAMS['order_first'], PARAMS['order_every']
        if (self.order is None and self.turn >= t0 and (self.turn - t0) % every == 0
                and self.turn + 1 < self.max_turns):
            rect = self.orders[self.order_idx % len(self.orders)]
            self.order_idx += 1
            self.order = {'no': self.order_idx, 'rect': list(rect), 'until': self.turn + PARAMS['order_duration']}
            ev.append({'t': 'orderspawn', 'no': self.order_idx})

    def _boost_step(self, ev):
        """加速道具（第 9 步）：剩余回合减 1（本回合刚拾取的除外），再处理出现与预告。"""
        for s in (0, 1):
            b = self.beams[s]
            if b.boost_left > 0 and not self.picked[s]:
                b.boost_left -= 1
        t, N, lead = self.turn, PARAMS['boost_every'], PARAMS['boost_lead']
        nb = self.boost_next
        if nb and t >= nb['at']:
            if self.boost:
                ev.append({'t': 'boostgone', 'x': self.boost['x'], 'y': self.boost['y']})
            self.boost = {'x': nb['x'], 'y': nb['y']}
            self.boost_next = None
            ev.append({'t': 'boostspawn', 'x': nb['x'], 'y': nb['y']})
        at = t + lead
        if at % N == 0 and at >= N and at < self.max_turns:
            p = self.boost_points[self.boost_idx % len(self.boost_points)]
            self.boost_idx += 1
            self.boost_next = {'x': p[0], 'y': p[1], 'at': at}
            ev.append({'t': 'boostannounce', 'x': p[0], 'y': p[1], 'at': at})

    # ------------------------------------------------------------ 一回合
    def step(self, cmds):
        """结算一回合。cmds 为两方命令，例如 ['U', 'L+']；无效或缺失（None）按原方向前进 1 格。
        '+' 只在加速状态下有效（走 2 格），否则被忽略。"""
        if self.done:
            return []
        ev = []
        self.turn += 1
        self.picked = [False, False]
        cut = [False, False]
        mv = [0, 0]
        # 第 1 步 确定方向，以及走 1 格还是 2 格
        for s in (0, 1):
            b = self.beams[s]
            if not b.alive:
                continue
            c = cmds[s] if isinstance(cmds[s], str) else ''
            d = c[:1]
            plus = c[1:] == '+'
            if len(c) > 2 or d not in DX or (len(c) == 2 and not plus) or d == OPP[b.dir]:
                d, plus = b.dir, False                     # 无效命令、掉头命令
            if b.boost_left <= 0:
                plus = False                               # 没有加速状态：+ 被忽略
            b.dir = d
            b.speed = 2 if plus else 1
            mv[s] = b.speed
        # 第 2～6 步 移动步（加速并下达 + 的束斑做两次）
        for sub in (1, 2):
            movers = [s for s in (0, 1) if self.beams[s].alive and not cut[s] and mv[s] >= sub]
            if movers:
                self._substep(movers, cut, ev)
        # 第 7 步 报废与补标
        restart_cut = [False, False]
        for s in (0, 1):
            if not cut[s]:
                continue
            for k in range(len(self.trail)):
                if self.trail[k] == s:
                    self.trail[k] = -1
            b = self.beams[s]
            b.alive, b.respawn, b.speed = False, PARAMS['restart'], 1
            b.boost_left = 0
            ev.append({'t': 'down', 's': s})
        for s in (0, 1):
            b = self.beams[s]
            if b.alive or cut[s]:
                continue
            b.respawn -= 1
            if b.respawn > 0:
                continue
            cx, cy = self.cores[s]
            b.alive, b.x, b.y, b.dir, b.speed, b.respawn = True, cx, cy, ('U' if s == 0 else 'D'), 1, 0
            if self.trail[self.idx(cx, cy)] == 1 - s:      # 重新启动视为移入该格
                restart_cut[1 - s] = True
            ev.append({'t': 'respawn', 's': s})
        for s in (0, 1):
            if not restart_cut[s] or not self.beams[s].alive:
                continue
            for k in range(len(self.trail)):
                if self.trail[k] == s:
                    self.trail[k] = -1
            b = self.beams[s]
            b.alive, b.respawn, b.speed = False, PARAMS['restart'], 1
            b.boost_left = 0
            ev.append({'t': 'down', 's': s})
        for s in (0, 1):                                    # 补标轮廓
            b = self.beams[s]
            if not b.alive:
                continue
            k = self.idx(b.x, b.y)
            if self.owner[k] != s and self.trail[k] != s:
                self.trail[k] = s
        # 决赛第 8 步 显影（初赛没有潜像，regions 为空）
        dev = [0, 0]
        for rid in list(self.regions.keys()):
            r = self.regions[rid]
            r[1] -= 1
            if r[1] > 0:
                continue
            n = 0
            for k in range(len(self.lat)):
                if self.lat[k] == rid:
                    self.lat[k] = 0
                    n += 1
            dev[r[0]] += n
            del self.regions[rid]
        for s in (0, 1):
            if dev[s]:
                ev.append({'t': 'develop', 's': s, 'n': dev[s]})
        # 决赛第 9 步 接地
        if self.final:
            for s in (0, 1):
                lost = self._ground(s)
                if lost:
                    ev.append({'t': 'decay', 's': s, 'n': lost})
        # 第 8 步 订单
        if self.orders:
            self._order_step(ev)
        # 第 9 步 道具
        if self.boost_points:
            self._boost_step(ev)
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


# Technical forfeits share the same rules for live matches and replay verification.
REQUEST_STATES = ('ok', 'timeout', 'connection', 'invalid', 'crash')
STARTUP_STATES = ('ready', 'startup', 'crash')
TURN_ERRORS = ('timeout', 'connection', 'invalid')
TIME_LIMIT_STATES = ('timeout', 'connection')


def _forfeit(game, failures):
    losers = [seat for seat, reason in enumerate(failures) if reason]
    if len(losers) == 1:
        loser = losers[0]
        game.forfeit(loser, failures[loser])
    elif losers:
        game.forfeit(-1, 'crash' if 'crash' in failures else failures[0])


def apply_startup(game, errors, startup, initial, exits):
    failures = []
    for seat, (state, init, exited) in enumerate(zip(startup, initial, exits)):
        failure = None
        if exited or state == 'crash' or init == 'crash':
            failure = 'crash'
        elif state != 'ready':
            failure = 'startup'
        elif init is not None and init != 'ok':
            errors[seat] += 1
        failures.append(failure)
    _forfeit(game, failures)
    return failures


def apply_turn(game, errors, statuses, us, exits):
    failures = []
    for seat, (status, elapsed, exited) in enumerate(zip(statuses, us, exits)):
        # Both issued requests count, including the round that triggers a forfeit.
        game.time_us[seat] += elapsed
        if status in TURN_ERRORS:
            errors[seat] += 1
        if exited or status == 'crash':
            failures.append('crash')
        else:
            failures.append('errors' if errors[seat] >= PARAMS['max_errors'] else None)
    _forfeit(game, failures)
