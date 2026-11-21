"""Encode the version 1 Bot messages and parse turn replies."""
import json

from .rules import DX

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 16384


def json_object(body):
    def bad_constant(value):
        raise ValueError('invalid JSON constant: ' + value)

    obj = json.loads(body.decode('utf-8'), parse_constant=bad_constant)
    if not isinstance(obj, dict):
        raise ValueError('expected a JSON object')
    return obj


def init_message(game, seat):
    return {
        'type': 'init', 'seat': seat, 'stage': game.stage, 'map': game.map['id'],
        'size': game.N, 'max_turns': game.max_turns,
        'terrain': list(game.map['terrain']),
        'calib': [list(c) for c in game.cores],
        'orders': [list(r) for r in game.orders],
        'boost_points': [list(p) for p in game.boost_points],
    }


def grid_rows(game):
    """owner / contour 两张网格，每行一个字符串。"""
    N = game.N
    own, con = [], []
    for y in range(N):
        o, c = [], []
        for x in range(N):
            k = y * N + x
            w = game.owner[k]
            o.append('.' if w < 0 else str(w))
            t = game.trail[k]
            c.append('.' if t < 0 else str(t))
        own.append(''.join(o)); con.append(''.join(c))
    return own, con


def board_status(game):
    o = game.order
    return {
        'beams': [b.to_dict(s) for s, b in enumerate(game.beams)],
        'order': {'no': o['no'], 'rect': list(o['rect']), 'until': o['until']} if o else None,
        'boost': dict(game.boost) if game.boost else None,
        'boost_next': dict(game.boost_next) if game.boost_next else None,
        'score': [game.score(0), game.score(1)],
        'bonus': list(game.bonus),
    }


def turn_message(game):
    own, con = grid_rows(game)
    return {'type': 'turn', 'turn': game.turn + 1, 'owner': own, 'contour': con, **board_status(game)}


def end_message(game):
    return {'type': 'end', 'turn': game.turn, 'winner': game.winner, 'reason': game.reason,
            'score': [game.score(0), game.score(1)], 'bonus': list(game.bonus)}


def dumps(msg):
    return json.dumps(msg, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def parse_reply(body):
    """解析回合回复（str 或 bytes）。返回 (turn, cmd)；格式不对返回 None。cmd 只接受 U/D/L/R，可带 +。"""
    try:
        obj = json_object(body.encode('utf-8') if isinstance(body, str) else body)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    t, cmd = obj.get('turn'), obj.get('cmd')
    if not isinstance(t, int) or isinstance(t, bool) or not isinstance(cmd, str):
        return None
    if len(cmd) not in (1, 2) or cmd[0] not in DX or (len(cmd) == 2 and cmd[1] != '+'):
        return None
    return t, cmd
