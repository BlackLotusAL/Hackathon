"""Round-end order settlement and boost scheduling."""
from .rules import PARAMS


def _order_cells(game, rect):
    return [game.idx(x, y) for y in range(rect[1], rect[3] + 1) for x in range(rect[0], rect[2] + 1)
            if game.terrain[game.idx(x, y)] != '#']

def settle_orders(game, ev):
    """客户订单（第 8 步）。先结算到期订单，再公示新订单。"""
    o = game.order
    if o and game.turn >= o['until']:
        got = [0, 0]
        for k in _order_cells(game, o['rect']):
            w = game.owner[k]
            if w >= 0:
                b = game.val(k) * PARAMS['order_bonus']
                game.bonus[w] += b
                got[w] += b
        ev.append({'t': 'orderexpire', 'no': o['no'], 'got': got})
        game.order = None
    t0, every = PARAMS['order_first'], PARAMS['order_every']
    if (game.order is None and game.turn >= t0 and (game.turn - t0) % every == 0
            and game.turn + 1 < game.max_turns):
        rect = game.orders[game.order_idx % len(game.orders)]
        game.order_idx += 1
        game.order = {'no': game.order_idx, 'rect': list(rect), 'until': game.turn + PARAMS['order_duration']}
        ev.append({'t': 'orderspawn', 'no': game.order_idx})

def update_boost(game, ev):
    """加速道具（第 9 步）：剩余回合减 1（本回合刚拾取的除外），再处理出现与预告。"""
    for s in (0, 1):
        b = game.beams[s]
        if b.boost_left > 0 and not game.picked[s]:
            b.boost_left -= 1
    t, N, lead = game.turn, PARAMS['boost_every'], PARAMS['boost_lead']
    nb = game.boost_next
    if nb and t >= nb['at']:
        if game.boost:
            ev.append({'t': 'boostgone', 'x': game.boost['x'], 'y': game.boost['y']})
        game.boost = {'x': nb['x'], 'y': nb['y']}
        game.boost_next = None
        ev.append({'t': 'boostspawn', 'x': nb['x'], 'y': nb['y']})
    at = t + lead
    if at % N == 0 and at >= N and at < game.max_turns:
        p = game.boost_points[game.boost_idx % len(game.boost_points)]
        game.boost_idx += 1
        game.boost_next = {'x': p[0], 'y': p[1], 'at': at}
        ev.append({'t': 'boostannounce', 'x': p[0], 'y': p[1], 'at': at})
