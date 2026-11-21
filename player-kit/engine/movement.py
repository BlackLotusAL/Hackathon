"""Resolve simultaneous beam movement, collisions, and restarts."""
from .rules import DX, DY, OPP, PARAMS


def move_beams(game, cmds, ev):
    cut = [False, False]
    mv = [0, 0]
    # 第 1 步 确定方向，以及走 1 格还是 2 格
    for s in (0, 1):
        b = game.beams[s]
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
        movers = [s for s in (0, 1) if game.beams[s].alive and not cut[s] and mv[s] >= sub]
        if movers:
            _move_one_step(game, movers, cut, ev)
    # 第 7 步 报废与补标
    restart_cut = [False, False]
    for s in (0, 1):
        if not cut[s]:
            continue
        for k in range(len(game.trail)):
            if game.trail[k] == s:
                game.trail[k] = -1
        b = game.beams[s]
        b.alive, b.respawn, b.speed = False, PARAMS['restart'], 1
        b.boost_left = 0
        ev.append({'t': 'down', 's': s})
    for s in (0, 1):
        b = game.beams[s]
        if b.alive or cut[s]:
            continue
        b.respawn -= 1
        if b.respawn > 0:
            continue
        cx, cy = game.cores[s]
        b.alive, b.x, b.y, b.dir, b.speed, b.respawn = True, cx, cy, ('U' if s == 0 else 'D'), 1, 0
        if game.trail[game.idx(cx, cy)] == 1 - s:      # 重新启动视为移入该格
            restart_cut[1 - s] = True
        ev.append({'t': 'respawn', 's': s})
    for s in (0, 1):
        if not restart_cut[s] or not game.beams[s].alive:
            continue
        for k in range(len(game.trail)):
            if game.trail[k] == s:
                game.trail[k] = -1
        b = game.beams[s]
        b.alive, b.respawn, b.speed = False, PARAMS['restart'], 1
        b.boost_left = 0
        ev.append({'t': 'down', 's': s})
    for s in (0, 1):                                    # 补标轮廓
        b = game.beams[s]
        if not b.alive:
            continue
        k = game.idx(b.x, b.y)
        if game.owner[k] != s and game.trail[k] != s:
            game.trail[k] = s


def _move_one_step(game, movers, cut, ev):
    """一个移动步（第 2～6 步）。movers 为本移动步移动的座位。"""
    start_trail = game.trail[:]
    tx, ty = [0, 0], [0, 0]
    moving = [False, False]
    # 第 2 步 撞缺陷与出界
    for s in movers:
        b = game.beams[s]
        tx[s], ty[s] = b.x + DX[b.dir], b.y + DY[b.dir]
        moving[s] = True
        if not game.inb(tx[s], ty[s]) or game.terrain[game.idx(tx[s], ty[s])] == '#':
            cut[s] = True
            moving[s] = False
            ev.append({'t': 'crash', 's': s})
    # 第 3 步 束斑碰撞
    if moving[0] and moving[1]:
        a, b = game.beams
        same = tx[0] == tx[1] and ty[0] == ty[1]
        swap = tx[0] == b.x and ty[0] == b.y and tx[1] == a.x and ty[1] == a.y
        if same or swap:
            for s in (0, 1):
                if game.owner[game.idx(tx[s], ty[s])] != s:
                    cut[s] = True
                    moving[s] = False
            ev.append({'t': 'collide'})
    # 第 4 步 移动
    for s in (0, 1):
        if moving[s]:
            game.beams[s].x, game.beams[s].y = tx[s], ty[s]
    # 第 5 步 扫过轮廓（按本移动步开始时的轮廓判断）
    for s in (0, 1):
        if not moving[s]:
            continue
        k = game.idx(game.beams[s].x, game.beams[s].y)
        tr = start_trail[k]
        if tr == s:
            cut[s] = True
            ev.append({'t': 'selfcut', 's': s})
        elif tr == 1 - s:
            cut[1 - s] = True
            ev.append({'t': 'cut', 's': s})
    # 第 6 步 闭合与延伸
    closers = []
    for s in (0, 1):
        if not moving[s] or cut[s]:
            continue
        k = game.idx(game.beams[s].x, game.beams[s].y)
        if game.owner[k] == s and s in game.trail:
            closers.append(s)
    fills, claim = {}, {}
    for s in closers:   # 双方都按闭合前的局面计算
        fills[s] = game.fill_cells(s)
        for c in fills[s]:
            claim[c] = claim.get(c, 0) | (1 << s)
    both = len(closers) == 2
    for s in closers:
        value = 0
        for c in fills[s]:
            if game.trail[c] == s:
                game.trail[c] = -1
            if game.base[c] >= 0 or game.owner[c] == s:
                continue
            if both and claim[c] == 3:
                continue          # 被双方同时围到：保持原归属
            if not both and game.trail[c] == 1 - s:
                cut[1 - s] = True
                ev.append({'t': 'enclose', 's': s})
            game.owner[c] = s
            value += game.val(c)
        for q in range(len(game.trail)):
            if game.trail[q] == s:
                game.trail[q] = -1
        ev.append({'t': 'close', 's': s, 'gain': value})
    for s in (0, 1):
        if not moving[s] or cut[s]:
            continue
        k = game.idx(game.beams[s].x, game.beams[s].y)
        if game.owner[k] != s:
            game.trail[k] = s
    if game.boost:                                     # 拾取道具（闭合结算之后）
        for s in (0, 1):
            b = game.beams[s]
            if moving[s] and not cut[s] and b.x == game.boost['x'] and b.y == game.boost['y']:
                b.boost_left = PARAMS['boost_duration']
                game.picked[s] = True
                ev.append({'t': 'pickup', 's': s, 'x': b.x, 'y': b.y})
                game.boost = None
                break
