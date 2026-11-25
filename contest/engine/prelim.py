"""Preliminary scoring and simultaneous territory capture."""
from functools import lru_cache


TIEBREAK_METRICS = ('score', 'lead_turns', 'time_us')


@lru_cache(maxsize=8)
def neighbors(size):
    return tuple(tuple(ny * size + nx for nx, ny in
                       ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                       if 0 <= nx < size and 0 <= ny < size)
                 for y in range(size) for x in range(size))


def territory_value(game, side):
    face = edges = 0
    owner = game.owner
    adjacent = neighbors(game.map.size)
    base = game.map.base
    for k, seat in enumerate(owner):
        if seat == side:
            face += game.val(k)
            if base[k] != side:
                edges += 4 - sum(owner[n] == side for n in adjacent[k])
    return face, edges


def score(game, side):
    face, edges = territory_value(game, side)
    return face - edges // 3


def extend_and_close(game, moving, cut, events):
    closers = []
    for seat, beam in enumerate(game.beams):
        if moving[seat] and not cut[seat]:
            k = game.idx(beam.x, beam.y)
            if game.owner[k] != seat:
                game.trail[k] = seat
            elif seat in game.trail:
                closers.append(seat)

    claims = {s: set(game.fill_cells(s)) for s in closers}
    shared = claims[0] & claims[1] if len(closers) == 2 else set()
    for s in closers:
        captured = claims[s] - shared
        for k in captured:
            game.owner[k] = s
        events.append({'t': 'close', 's': s, 'gain': sum(game.val(k) for k in captured)})
    if closers:
        for k, s in enumerate(game.trail):
            if s in claims:
                game.trail[k] = -1
