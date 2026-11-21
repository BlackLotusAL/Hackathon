"""Encode the public replay produced by a formal match."""
from . import VERSION
from .core import PARAMS, PRELIM_PARAMS


def replay_map(board):
    keys = ('id', 'name', 'en', 'stage', 'size', 'calib', 'terrain', 'orders', 'boost_points')
    return {key: board[key] for key in keys if key in board}


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
    owner = ''.join('.' if seat < 0 else str(seat) for seat in game.owner)
    contour = ''.join('.' if seat < 0 else str(seat) for seat in game.trail)
    if game.final:
        latent = ''.join(str(game.regions[region][1]) if region else '.' for region in game.lat)
        return owner, contour, latent
    return owner, contour


def _sides(game):
    return {
        'beams': [beam.to_dict(seat) for seat, beam in enumerate(game.beams)],
        'score': [game.score(0), game.score(1)],
        'bonus': list(game.bonus),
    }


class Recorder:
    def __init__(self, game, players):
        self.game = game
        self.previous = _cells(game)
        layers = ('owner', 'contour', 'latent')
        initial = dict(zip(layers, self.previous))
        initial.update(_sides(game))
        self.data = {
            'version': VERSION,
            'map': replay_map(game.map),
            'stage': game.stage,
            'players': list(players),
            'params': dict(PARAMS if game.final else PRELIM_PARAMS),
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
            **_sides(game),
            'order': dict(game.order) if game.order else None,
            'boost': dict(game.boost) if game.boost else None,
            'boost_next': dict(game.boost_next) if game.boost_next else None,
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
