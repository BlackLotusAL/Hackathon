"""Typed snapshots supplied to Python strategies."""
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional

from engine.game import Beam as GameBeam
from engine.game import Game
from engine.rules import RULESET
from engine.prelim import active_station, step_count


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class InitState:
    seat: int
    size: int
    max_turns: int
    stage: str
    map: str
    terrain: tuple[str, ...]
    calib: tuple[Point, Point]
    boost_points: tuple[Point, ...]

    @classmethod
    def from_message(cls, msg: dict) -> 'InitState':
        if msg['stage'] != RULESET:
            raise ValueError('Bot 只接受当前赛题规则')
        return cls(msg['seat'], msg['size'], msg['max_turns'], msg['stage'], msg['map'],
                   tuple(msg['terrain']), tuple(Point(*p) for p in msg['calib']),
                   tuple(Point(*p) for p in msg['boost_points']))


@dataclass(frozen=True)
class Beam:
    seat: int
    x: int
    y: int
    dir: str
    speed: int
    alive: bool
    restart_in: int


@dataclass(frozen=True)
class TurnState:
    turn: int
    owner: tuple[str, ...]
    contour: tuple[str, ...]
    beams: tuple[Beam, Beam]
    score: tuple[int, int]
    lead_turns: tuple[int, int]
    time_us: tuple[int, int]
    _init: InitState = field(repr=False, compare=False)
    _init_message: dict = field(repr=False, compare=False)
    _message: dict = field(repr=False, compare=False)

    @classmethod
    def from_message(cls, init: InitState, init_message: dict, msg: dict) -> 'TurnState':
        beam_fields = ('seat', 'x', 'y', 'dir', 'speed', 'alive', 'restart_in')
        return cls(msg['turn'], tuple(msg['owner']), tuple(msg['contour']),
                   tuple(Beam(*(b[k] for k in beam_fields)) for b in msg['beams']),
                   tuple(msg['score']), tuple(msg['lead_turns']), tuple(msg['time_us']), init, init_message, msg)

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
    def active_station(self) -> Optional[Point]:
        return active_station(self._init.boost_points, self.turn)

    def move_steps(self, beam: Beam) -> int:
        station = self.active_station
        return step_count((station.x, station.y) if station else None, beam.x, beam.y)

    @cached_property
    def game(self) -> Game:
        """Build the optional simulator on first access."""
        return build_game(self._init_message, self._message)


def build_game(info, msg):
    """由开局消息和回合消息还原出与平台一致的局面（turn 设为已结算的回合数）。"""
    m = {'id': info['map'], 'size': info['size'], 'terrain': info['terrain'], 'calib': info['calib'],
         'boost_points': info['boost_points'], 'stage': info['stage']}
    g = Game(m, max_turns=info['max_turns'])
    own, con = ''.join(msg['owner']), ''.join(msg['contour'])
    g.owner = [-1 if c == '.' else int(c) for c in own]
    g.trail = [-1 if c == '.' else int(c) for c in con]
    for b in msg['beams']:
        x = GameBeam(b['x'], b['y'], b['dir'])
        x.speed, x.alive, x.respawn = b['speed'], b['alive'], b.get('restart_in', 0)
        g.beams[b['seat']] = x
    g.turn = msg['turn'] - 1
    g.lead_turns = list(msg['lead_turns'])
    g.time_us = list(msg['time_us'])
    return g
