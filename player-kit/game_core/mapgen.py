"""随机地图生成器与地图约束校验（规则文档第 3 节）。

按规则文档第 3 节"每张地图都满足的约束"随机生成地图，用来练习和测试；validate() 逐条检查这些约束。

用法：
  m = mapgen.generate(42)                # 同一个种子总是得到同一张图；地图编号为 R42
  m = mapgen.get('R42')                  # 按编号取图
  errors = mapgen.validate(m)            # 空列表表示满足全部约束

"""
import hashlib
import random
import re
from collections import deque

from .protocol import dumps, json_object

N = 41
C = N - 1                      # 旋转中心：(x, y) <-> (C - x, C - y)
STYLES = ('open', 'walls', 'lanes', 'blocks', 'mixed')


def rot(x, y):
    return C - x, C - y


def bfs(t, src):
    """t 为 41 个字符串（或字符列表的列表）。返回 {(x, y): 路程}。"""
    dist = {src: 0}
    q = deque([src])
    while q:
        x, y = q.popleft()
        d = dist[(x, y)] + 1
        for a, b in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= a < N and 0 <= b < N and t[b][a] != '#' and (a, b) not in dist:
                dist[(a, b)] = d
                q.append((a, b))
    return dist


def near_calib(x, y, calib, r=4):
    return any(abs(x - cx) <= r and abs(y - cy) <= r for cx, cy in calib)


# ---------------------------------------------------------------- 校验
def validate(m):
    """检查一张地图是否满足第 3 节的全部约束，返回错误列表（空列表表示通过）。"""
    err = []
    t = m.get('terrain') or []
    if m.get('size') != N or len(t) != N or any(len(r) != N for r in t):
        return ['尺寸不是 41×41']
    if any(c not in '.#*' for r in t for c in r):
        err.append('出现 . # * 以外的字符')
    for y in range(N):
        for x in range(N):
            if t[y][x] != t[C - y][C - x]:
                err.append('不对称：(%d,%d)' % (x, y))
                break
        if err and err[-1].startswith('不对称'):
            break
    c0, c1 = [tuple(c) for c in m['calib']]
    if rot(*c0) != c1:
        err.append('两个校准区不是 180° 旋转关系')
    free = sum(1 for r in t for c in r if c != '#')
    d0, d1 = bfs(t, c0), bfs(t, c1)
    if len(d0) != free:
        err.append('非缺陷格不连通：%d / %d' % (len(d0), free))
    for cx, cy in (c0, c1):
        for y in range(cy - 4, cy + 5):
            for x in range(cx - 4, cx + 5):
                inner = abs(x - cx) <= 1 and abs(y - cy) <= 1
                if not (0 <= x < N and 0 <= y < N):
                    if inner:
                        err.append('校准区出界')
                    continue
                if t[y][x] == '#':
                    err.append('校准区 (%d,%d) 周围 4 格内有缺陷 (%d,%d)' % (cx, cy, x, y))
                elif inner and t[y][x] != '.':
                    err.append('校准区 3×3 内不是空白格 (%d,%d)' % (x, y))
    orders = m.get('orders') or []
    if not 6 <= len(orders) <= 8 or len(orders) % 2:
        err.append('订单区域数量 %d 不在 6～8 或不是偶数' % len(orders))
    for i, r in enumerate(orders):
        x0, y0, x1, y1 = r
        if x1 - x0 != 5 or y1 - y0 != 5 or x0 < 0 or y0 < 0 or x1 >= N or y1 >= N:
            err.append('订单 %d 不是基板内的 6×6 区域' % (i + 1))
            continue
        if i % 2 == 1:
            p = orders[i - 1]
            if [C - p[2], C - p[3], C - p[0], C - p[1]] != list(r):
                err.append('订单 %d 不是订单 %d 的旋转' % (i + 1, i))
        if sum(1 for y in range(y0, y1 + 1) for x in range(x0, x1 + 1) if t[y][x] != '#') < 18:
            err.append('订单 %d 缺陷过多' % (i + 1))
    bps = m.get('boost_points') or []
    if len(bps) != 3:
        err.append('道具刷新点不是 3 个')
    eq = [k for k in d0 if d0[k] == d1.get(k) and not near_calib(k[0], k[1], (c0, c1))]
    dmin = min([d0[k] for k in eq] or [0])
    for p in bps:
        x, y = p
        if not (0 <= x < N and 0 <= y < N) or t[y][x] == '#':
            err.append('刷新点 %s 在缺陷上或出界' % p)
            continue
        if near_calib(x, y, (c0, c1)):
            err.append('刷新点 %s 在校准区周围 4 格内' % p)
        if d0.get((x, y)) != d1.get((x, y)):
            err.append('刷新点 %s 到双方路程不等' % p)
        elif d0.get((x, y), 0) > dmin + 2:
            err.append('刷新点 %s 比最近的等距格远了 %d 步' % (p, d0[(x, y)] - dmin))
    return err


# ---------------------------------------------------------------- 生成
def _pieces(rng, style):
    """返回一个缺陷形状（相对坐标列表）。"""
    k = rng.random()
    if style == 'lanes' and k < 0.7:
        n = rng.randint(7, 15)
        return [(i, 0) for i in range(n)] if rng.random() < 0.5 else [(0, i) for i in range(n)]
    if style == 'walls' and k < 0.6:
        n = rng.randint(3, 9)
        seg = [(i, 0) for i in range(n)] if rng.random() < 0.5 else [(0, i) for i in range(n)]
        if rng.random() < 0.4:                       # L 形
            m = rng.randint(2, 5)
            ex, ey = seg[-1]
            seg += [(ex, ey + j) for j in range(1, m)] if ey == 0 else [(ex + j, ey) for j in range(1, m)]
        return seg
    if style == 'blocks' and k < 0.7:
        w, h = rng.randint(2, 4), rng.randint(2, 4)
        return [(i, j) for i in range(w) for j in range(h)]
    if k < 0.35:
        return [(0, 0), (1, 0)] if rng.random() < 0.5 else [(0, 0), (0, 1)]
    if k < 0.6:
        return [(0, 0), (1, 0), (0, 1), (1, 1)]
    n = rng.randint(2, 6)
    return [(i, 0) for i in range(n)] if rng.random() < 0.5 else [(0, i) for i in range(n)]


def _keys(rng):
    k = rng.random()
    if k < 0.35:
        return [(0, 0), (1, 0), (0, 1), (1, 1)]
    if k < 0.55:
        return [(i, j) for i in range(3) for j in range(3)]
    if k < 0.75:
        return [(1, 0), (0, 1), (1, 1), (2, 1), (1, 2)]
    return [(0, 0), (1, 0), (2, 0)] if rng.random() < 0.5 else [(0, 0), (0, 1), (0, 2)]


def _try(rng, stage):
    style = rng.choice(STYLES)
    density = {'open': (0.004, 0.02), 'walls': (0.02, 0.07), 'lanes': (0.03, 0.07),
               'blocks': (0.03, 0.09), 'mixed': (0.02, 0.06)}[style]
    while True:
        cx, cy = rng.randint(5, 15), rng.randint(25, 35)
        if abs(cx - C / 2) + abs(cy - C / 2) >= 10:            # 两个校准区至少隔开 20 步左右
            break
    calib = [(cx, cy), rot(cx, cy)]
    g = [['.'] * N for _ in range(N)]
    target = int(N * N * rng.uniform(*density))
    count, tries = 0, 0
    total_free = N * N
    while count < target and tries < 400:
        tries += 1
        shape = _pieces(rng, style)
        ox, oy = rng.randint(0, C), rng.randint(0, C)
        cells = set()
        for dx, dy in shape:
            x, y = ox + dx, oy + dy
            if not (0 <= x < N and 0 <= y < N):
                cells = None
                break
            cells.add((x, y))
            cells.add(rot(x, y))
        if not cells or any(near_calib(x, y, calib) or g[y][x] != '.' or (abs(x - 20) <= 1 and abs(y - 20) <= 1)
                            for x, y in cells):
            continue
        for x, y in cells:
            g[y][x] = '#'
        if len(bfs(g, calib[0])) != total_free - len(cells):
            for x, y in cells:
                g[y][x] = '.'
            continue
        total_free -= len(cells)
        count += len(cells)
    keys_target = rng.randint(14, 34)
    kc, tries = 0, 0
    while kc < keys_target and tries < 300:
        tries += 1
        shape = _keys(rng)
        ox, oy = rng.randint(1, C - 3), rng.randint(1, C - 3)
        cells = set()
        for dx, dy in shape:
            cells.add((ox + dx, oy + dy))
            cells.add(rot(ox + dx, oy + dy))
        if any(g[y][x] != '.' or near_calib(x, y, calib, 2) for x, y in cells):
            continue
        for x, y in cells:
            g[y][x] = '*'
        kc += len(cells)
    t = [''.join(r) for r in g]
    # 订单：成对的 6×6 区域
    orders, tries = [], 0
    pairs = rng.choice((3, 3, 4))
    while len(orders) < pairs * 2 and tries < 500:
        tries += 1
        x0, y0 = rng.randint(0, N - 6), rng.randint(0, N - 6)
        r = [x0, y0, x0 + 5, y0 + 5]
        rr = [C - r[2], C - r[3], C - r[0], C - r[1]]
        if r == rr or abs(x0 - rr[0]) < 6 and abs(y0 - rr[1]) < 6:
            continue                                   # 与自己的旋转重叠
        if any(abs(x0 - o[0]) < 4 and abs(y0 - o[1]) < 4 for o in orders):
            continue
        if any(near_calib(x, y, calib, 1) for x in range(r[0], r[2] + 1) for y in range(r[1], r[3] + 1)):
            continue
        if sum(1 for y in range(y0, y0 + 6) for x in range(x0, x0 + 6) if t[y][x] != '#') < 30:
            continue
        orders += [r, rr] if rng.random() < 0.5 else [rr, r]
    # 道具刷新点：等距、靠近双方的一带
    d0, d1 = bfs(t, calib[0]), bfs(t, calib[1])
    eq = [k for k in d0 if d0[k] == d1.get(k) and not near_calib(k[0], k[1], calib)]
    if not eq:
        return None
    dmin = min(d0[k] for k in eq)
    band = [k for k in eq if d0[k] <= dmin + 2]
    # 一对互为旋转的点（尽量分开），再加正中央或离这一对最远的一点
    pairs_ok = [k for k in band if k != rot(*k) and rot(*k) in band]
    rng.shuffle(pairs_ok)
    pairs_ok.sort(key=lambda k: -min(12, abs(2 * k[0] - C) + abs(2 * k[1] - C)))
    pts = []
    if pairs_ok:
        a = pairs_ok[rng.randrange(min(4, len(pairs_ok)))]
        pts = [a, rot(*a)]
    rest = [k for k in band if k not in pts]
    if (20, 20) in rest and rng.random() < 0.8:
        pts.append((20, 20))
    rest = [k for k in rest if k not in pts]
    rest.sort(key=lambda k: -min([abs(k[0] - p[0]) + abs(k[1] - p[1]) for p in pts] or [0]))
    pts += rest[:3 - len(pts)]
    if len(pts) < 3:
        return None
    pts = pts[:3]
    if rng.random() < 0.6 and (20, 20) in pts:          # 多数地图第一个道具出现在正中央
        pts.remove((20, 20))
        pts.insert(0, (20, 20))
    return {'size': N, 'stage': stage, 'calib': [list(c) for c in calib], 'terrain': t, 'orders': orders,
            'boost_points': [list(p) for p in pts], 'style': style}


def generate(seed=None, stage='prelim'):
    """生成一张满足第 3 节全部约束的地图。seed 相同则地图相同；不给 seed 时随机选一个。"""
    if stage != 'prelim':
        raise ValueError('此材料包适用初赛地图')
    if seed is None:
        seed = random.randrange(1, 10 ** 6)
    seed = int(seed)
    rng = random.Random('arena-map-%d' % seed)
    for _ in range(100):
        m = _try(rng, stage)
        if m and not validate(m):
            m.update({'id': 'R%d' % seed, 'name': '随机地图 %d' % seed, 'seed': seed})
            return m
    raise RuntimeError('种子 %d 生成失败' % seed)


def load_map(spec, stage):
    """Load the board in an Arena request and hash the bytes used by the judge."""
    if 'seed' in spec and 'file' not in spec and type(spec['seed']) is int:
        board = generate(spec['seed'], stage)
        digest = hashlib.sha256(dumps(board).encode('utf-8')).hexdigest()
    elif 'file' in spec and 'seed' not in spec and isinstance(spec['file'], str):
        raw = spec['file'].encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec.get('sha256'):
            raise ValueError('封存地图的 SHA-256 不匹配')
        board = json_object(raw)
        if board.get('stage') != stage or board.get('size') != N:
            raise ValueError('封存地图的赛段或尺寸不匹配')
        problems = validate(board)
        if problems:
            raise ValueError('封存地图无效：' + '；'.join(problems))
    else:
        raise ValueError('config.map 必须包含整数 seed 或封存地图 file 与 sha256')
    return board, digest


def get(name=None, stage='prelim', maps=None):
    """按编号取地图，优先使用传入的地图表，也支持按 R<种子> 生成地图。"""
    if stage != 'prelim':
        raise ValueError('此材料包适用初赛地图')
    if name in (None, '', 'random'):
        return generate(None, stage)
    if maps and name in maps:
        if maps[name].get('stage', 'prelim') != stage:
            raise ValueError('地图赛段与初赛材料包不符')
        return maps[name]
    random_map = re.fullmatch(r'[Rr](-?[0-9]+)', str(name))
    if random_map:
        return generate(int(random_map.group(1)), stage)
    raise KeyError('未知地图 %s（随机地图写作 R<种子>，例如 R42）' % name)


def draw(m):
    """把地图画成文字：0 / 1 校准区，# 缺陷，* 关键图形区，o 订单区域，+ 刷新点。"""
    g = [list(r) for r in m['terrain']]
    for r in m['orders']:
        for y in range(r[1], r[3] + 1):
            for x in range(r[0], r[2] + 1):
                if g[y][x] == '.':
                    g[y][x] = 'o'
    for s, (cx, cy) in enumerate(m['calib']):
        for y in range(cy - 1, cy + 2):
            for x in range(cx - 1, cx + 2):
                g[y][x] = str(s)
    for x, y in m['boost_points']:
        g[y][x] = '+'
    return '\n'.join(' '.join(r) for r in g)
