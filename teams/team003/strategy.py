from engine.rules import DIRS, DX, DY, OPP
from sdk import InitState, TurnState


class Strategy:
    def __init__(self, _init: InitState):
        pass

    def act(self, state: TurnState) -> str:
        me = state.me
        if not me.alive:
            return me.dir

        board = state.map
        steps = board.move_steps(me.x, me.y)
        own = str(state.seat)
        wants_owned_cell = state.owner[me.y][me.x] != own
        best_direction = me.dir
        best_priority = None
        for direction in DIRS:
            if direction == OPP[me.dir]:
                continue
            path = [(me.x + i * DX[direction], me.y + i * DY[direction])
                    for i in range(1, steps + 1)]
            if any(not (0 <= x < board.size and 0 <= y < board.size)
                   or board.terrain[y][x] == '#' or state.contour[y][x] == own for x, y in path):
                continue
            x, y = path[-1]
            next_cell_owned = state.owner[y][x] == own
            priority = (next_cell_owned == wants_owned_cell, direction == me.dir)
            if best_priority is None or priority > best_priority:
                best_direction, best_priority = direction, priority
        return best_direction
