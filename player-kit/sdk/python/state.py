"""Typed snapshots supplied to Python strategies."""
from dataclasses import dataclass, field
from functools import cached_property

from engine.board import GameMap
from engine.game import Beam as GameBeam
from engine.game import Game


@dataclass(frozen=True)
class InitState:
    seat: int
    max_turns: int
    map: GameMap

    @classmethod
    def from_message(cls, msg: dict) -> 'InitState':
        seat, max_turns = msg['seat'], msg['max_turns']
        if type(seat) is not int or seat not in (0, 1) or type(max_turns) is not int or max_turns < 1:
            raise ValueError('开局消息的出生点或回合上限无效')
        return cls(seat, max_turns, GameMap.from_payload(msg['map']))


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

    @classmethod
    def from_message(cls, init: InitState, msg: dict) -> 'TurnState':
        beam_fields = ('seat', 'x', 'y', 'dir', 'speed', 'alive', 'restart_in')
        return cls(msg['turn'], tuple(msg['owner']), tuple(msg['contour']),
                   tuple(Beam(*(b[k] for k in beam_fields)) for b in msg['beams']),
                   tuple(msg['score']), tuple(msg['lead_turns']), tuple(msg['time_us']), init)

    @property
    def map(self) -> GameMap:
        return self._init.map

    @property
    def seat(self) -> int:
        return self._init.seat

    @property
    def me(self) -> Beam:
        return self.beams[self.seat]

    @property
    def enemy(self) -> Beam:
        return self.beams[1 - self.seat]

    @cached_property
    def game(self) -> Game:
        """Restore the mutable match state while sharing the original map."""
        game = Game(self.map, max_turns=self._init.max_turns)
        game.owner = [-1 if cell == '.' else int(cell) for cell in ''.join(self.owner)]
        game.trail = [-1 if cell == '.' else int(cell) for cell in ''.join(self.contour)]
        for snapshot in self.beams:
            beam = GameBeam(snapshot.x, snapshot.y, snapshot.dir)
            beam.speed, beam.alive, beam.respawn = snapshot.speed, snapshot.alive, snapshot.restart_in
            game.beams[snapshot.seat] = beam
        game.turn = self.turn - 1
        game.lead_turns = list(self.lead_turns)
        game.time_us = list(self.time_us)
        return game
