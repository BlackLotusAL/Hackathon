"""Encode the public replay produced by a formal match."""
from . import VERSION
from .protocol import board_status, grid_rows
from .rules import PARAMS


def game_result(game):
    return {'winner': game.winner, 'reason': game.reason, 'turns': game.turn, **game.metrics()}


def public_observation(entry):
    """Keep rule inputs while omitting private responses and exit details."""
    keys = {
        'startup': ('phase', 'startup', 'init'),
        'end': ('phase', 'reason'),
        None: ('turn', 'status', 'us'),
    }[entry.get('phase')]
    result = {key: entry[key] for key in keys}
    if 'exits' in entry:
        result['exits'] = [info is not None for info in entry['exits']]
    return result


def _cells(game):
    owner, contour = grid_rows(game)
    return ''.join(owner), ''.join(contour)


class Recorder:
    def __init__(self, game, players):
        self.game = game
        self.previous = _cells(game)
        owner, contour = self.previous
        initial = {'owner': owner, 'contour': contour, **board_status(game)}
        self.data = {
            'version': VERSION,
            'map': game.map.to_payload(),
            'stage': game.map.stage,
            'players': list(players),
            'params': dict(PARAMS),
            'init': initial,
            'frames': [],
            'observations': [],
            'result': None,
        }

    def record(self, commands, events, ms=None):
        game = self.game
        cells = _cells(game)
        changes = []
        for index, (old, new) in enumerate(zip(zip(*self.previous), zip(*cells))):
            if old != new:
                changes.append([index, *new])
        self.previous = cells
        frame = {
            'turn': game.turn,
            'cmds': list(commands),
            **board_status(game),
            'events': events,
            'd': changes,
        }
        if ms is not None:
            frame['ms'] = list(ms)
        self.data['frames'].append(frame)
        return frame

    def finish(self):
        self.data['result'] = game_result(self.game)
        return self.data
