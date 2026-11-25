"""生成天梯随机地图并校验正式比赛的封存地图。"""
import hashlib
import random
from collections import deque

from .protocol import dumps, json_object
from .rules import RULESET

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
def _coordinate_lists(values, width):
    return isinstance(values, (list, tuple)) and all(
        isinstance(value, (list, tuple)) and len(value) == width and all(type(coord) is int for coord in value)
        for value in values
    )


def validate(m):
    """检查一张地图是否满足第 3 节的全部约束，返回错误列表（空列表表示通过）。"""
    err = []
    if m.get('stage', RULESET) != RULESET:
        return ['地图须使用当前赛题规则']
    t = m.get('terrain')
    if (m.get('size') != N or not isinstance(t, (list, tuple)) or len(t) != N
            or any(not isinstance(r, str) or len(r) != N for r in t)):
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
    calib = m.get('calib')
    if not _coordinate_lists(calib, 2) or len(calib) != 2:
        return err + ['必须提供两个校准区中心']
    c0, c1 = map(tuple, calib)
    if any(not (1 <= x < N - 1 and 1 <= y < N - 1) for x, y in (c0, c1)):
        return err + ['校准区出界']
    if rot(*c0) != c1:
        err.append('两个校准区不是 180° 旋转关系')
    if all(abs(a - b) <= 2 for a, b in zip(c0, c1)):
        err.append('两个 3×3 校准区重叠')
    free = sum(1 for r in t for c in r if c != '#')
    d0, d1 = bfs(t, c0), bfs(t, c1)
    if len(d0) != free:
        err.append('非缺陷格不连通：%d / %d' % (len(d0), free))
    for cx, cy in (c0, c1):
        for y in range(cy - 4, cy + 5):
            for x in range(cx - 4, cx + 5):
                inner = abs(x - cx) <= 1 and abs(y - cy) <= 1
                if not (0 <= x < N and 0 <= y < N):
                    continue
                if t[y][x] == '#':
                    err.append('校准区 (%d,%d) 周围 4 格内有缺陷 (%d,%d)' % (cx, cy, x, y))
                elif inner and t[y][x] != '.':
                    err.append('校准区 3×3 内不是空白格 (%d,%d)' % (x, y))
    bps = m.get('boost_points')
    if not _coordinate_lists(bps, 2):
        return err + ['加速站中心坐标无效']
    if len(bps) != 3 or len(set(map(tuple, bps))) != 3:
        err.append('加速站中心不是 3 个')
    eq = [k for k in d0 if d0[k] == d1.get(k)]
    dmin = min([d0[k] for k in eq] or [0])
    for p in bps:
        x, y = p
        if not (0 <= x < N and 0 <= y < N) or t[y][x] == '#':
            err.append('标记点 %s 在缺陷上或出界' % p)
            continue
        if near_calib(x, y, (c0, c1)):
            err.append('标记点 %s 在校准区周围 4 格内' % p)
        if d0.get((x, y)) != d1.get((x, y)):
            err.append('标记点 %s 到双方路程不等' % p)
        elif d0.get((x, y), 0) > dmin + 2:
            err.append('标记点 %s 比最近的等距格远了 %d 步' % (p, d0[(x, y)] - dmin))
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
    # 选取靠近双方的等距标记点
    d0, d1 = bfs(t, calib[0]), bfs(t, calib[1])
    eq = [k for k in d0 if d0[k] == d1.get(k)]
    if not eq:
        return None
    dmin = min(d0[k] for k in eq)
    band = [k for k in eq if d0[k] <= dmin + 2 and not near_calib(*k, calib)]
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
    if rng.random() < 0.6 and (20, 20) in pts:          # 多数地图的第一个标记点在正中央
        pts.remove((20, 20))
        pts.insert(0, (20, 20))
    return {'size': N, 'stage': stage, 'calib': [list(c) for c in calib], 'terrain': t,
            'boost_points': [list(p) for p in pts], 'style': style}


def generate(seed=None, stage=RULESET):
    """生成一张满足第 3 节全部约束的地图。seed 相同则地图相同；不给 seed 时随机选一个。"""
    if stage != RULESET:
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




def load_map(spec, stage=RULESET):
    """Load the board in an Arena request and hash the bytes used by the judge."""
    if stage != RULESET:
        raise ValueError('裁判只接受当前赛题规则')
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


def draw(m):
    """用 0、1 标记校准区，用 A、B、C 标记加速站。"""
    g = [list(r) for r in m['terrain']]
    for s, (cx, cy) in enumerate(m['calib']):
        for y in range(cy - 1, cy + 2):
            for x in range(cx - 1, cx + 2):
                g[y][x] = str(s)
    for station, (x, y) in enumerate(m['boost_points']):
        g[y][x] = 'ABC'[station]
    return '\n'.join(' '.join(r) for r in g)
