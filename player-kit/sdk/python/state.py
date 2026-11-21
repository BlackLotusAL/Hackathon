"""Typed snapshots supplied to Python strategies."""
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional

from engine.game import Beam as GameBeam
from engine.game import Game
from engine.rules import PARAMS


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    x0: int
    y0: int
    x1: int
    y1: int


@dataclass(frozen=True)
class InitState:
    seat: int
    size: int
    max_turns: int
    stage: str
    map: str
    terrain: tuple[str, ...]
    calib: tuple[Point, Point]
    orders: tuple[Rect, ...]
    boost_points: tuple[Point, ...]

    @classmethod
    def from_message(cls, msg: dict) -> 'InitState':
        return cls(msg['seat'], msg['size'], msg['max_turns'], msg['stage'], msg['map'],
                   tuple(msg['terrain']), tuple(Point(*p) for p in msg['calib']),
                   tuple(Rect(*r) for r in msg['orders']), tuple(Point(*p) for p in msg['boost_points']))


@dataclass(frozen=True)
class Beam:
    seat: int
    x: int
    y: int
    dir: str
    speed: int
    alive: bool
    restart_in: int
    boost_left: int


@dataclass(frozen=True)
class Order:
    no: int
    rect: Rect
    until: int


@dataclass(frozen=True)
class NextBoost:
    x: int
    y: int
    at: int


@dataclass(frozen=True)
class TurnState:
    turn: int
    owner: tuple[str, ...]
    contour: tuple[str, ...]
    beams: tuple[Beam, Beam]
    score: tuple[int, int]
    bonus: tuple[int, int]
    order: Optional[Order]
    boost: Optional[Point]
    boost_next: Optional[NextBoost]
    _init: InitState = field(repr=False, compare=False)
    _init_message: dict = field(repr=False, compare=False)
    _message: dict = field(repr=False, compare=False)

    @classmethod
    def from_message(cls, init: InitState, init_message: dict, msg: dict) -> 'TurnState':
        order, boost, next_boost = msg['order'], msg['boost'], msg['boost_next']
        beam_fields = ('seat', 'x', 'y', 'dir', 'speed', 'alive', 'restart_in', 'boost_left')
        return cls(msg['turn'], tuple(msg['owner']), tuple(msg['contour']),
                   tuple(Beam(*(b[k] for k in beam_fields)) for b in msg['beams']),
                   tuple(msg['score']), tuple(msg['bonus']),
                   Order(order['no'], Rect(*order['rect']), order['until']) if order is not None else None,
                   Point(boost['x'], boost['y']) if boost is not None else None,
                   NextBoost(next_boost['x'], next_boost['y'], next_boost['at']) if next_boost is not None else None,
                   init, init_message, msg)

    @property
    def seat(self) -> int:
        return self._init.seat

    @property
    def me(self) -> Beam:
        return self.beams[self.seat]

    @property
    def enemy(self) -> Beam:
        return self.beams[1 - self.seat]

    @property
    def latent(self) -> tuple[str, ...]:
        """Final-stage countdown rows, empty in preliminary matches."""
        return tuple(self._message.get('latent', ()))

    @cached_property
    def game(self) -> Game:
        """Build the optional simulator on first access."""
        return build_game(self._init_message, self._message)


def build_game(info, msg):
    """由开局消息和回合消息还原出与平台一致的局面（turn 设为已结算的回合数）。"""
    m = {'id': info['map'], 'size': info['size'], 'terrain': info['terrain'], 'calib': info['calib'],
         'orders': info.get('orders', []), 'boost_points': info.get('boost_points', []),
         'stage': info['stage']}
    g = Game(m, stage=info['stage'], max_turns=info['max_turns'])
    own, con = ''.join(msg['owner']), ''.join(msg['contour'])
    g.owner = [-1 if c == '.' else int(c) for c in own]
    g.trail = [-1 if c == '.' else int(c) for c in con]
    if g.stage == 'final':
        _restore_latent(g, msg)
    for b in msg['beams']:
        x = GameBeam(b['x'], b['y'], b['dir'])
        x.speed, x.alive, x.respawn = b['speed'], b['alive'], b.get('restart_in', 0)
        x.boost_left = b.get('boost_left', 0)
        g.beams[b['seat']] = x
    g.bonus = list(msg['bonus'])
    g.turn = msg['turn'] - 1
    o = msg.get('order')
    g.order = {'no': o['no'], 'rect': list(o['rect']), 'until': o['until']} if o else None
    t0, ev = PARAMS['order_first'], PARAMS['order_every']
    g.order_idx = 0 if g.turn < t0 else (g.turn - t0) // ev + 1
    g.boost = dict(msg['boost']) if msg.get('boost') else None
    g.boost_next = dict(msg['boost_next']) if msg.get('boost_next') else None
    bn, lead = PARAMS['boost_every'], PARAMS['boost_lead']
    g.boost_idx = max(0, min((g.turn + lead) // bn, (g.max_turns - 1) // bn))
    return g


def _restore_latent(game, message):
    countdowns = ''.join(message['latent'])
    for region, cells in message['latent_regions'].items():
        region = int(region)
        first = cells[0]
        game.regions[region] = [game.owner[first], int(countdowns[first])]
        for cell in cells:
            game.lat[cell] = region
    game.next_region = max(game.regions, default=0) + 1
