from engine.rules import DIRS, DX, DY, OPP
from sdk import InitState, TurnState


class Strategy:
    def __init__(self, init: InitState):
        self.init = init

    def act(self, state: TurnState) -> str:
        me = state.me
        if not me.alive:
            return me.dir

        own = str(state.seat)
        wants_owned_cell = state.owner[me.y][me.x] != own
        best_direction = me.dir
        best_priority = None
        for direction in DIRS:
            if direction == OPP[me.dir]:
                continue
            x, y = me.x + DX[direction], me.y + DY[direction]
            if (not (0 <= x < self.init.size and 0 <= y < self.init.size)
                    or self.init.terrain[y][x] == '#' or state.contour[y][x] == own):
                continue
            next_cell_owned = state.owner[y][x] == own
            priority = (next_cell_owned == wants_owned_cell, direction == me.dir)
            if best_priority is None or priority > best_priority:
                best_direction, best_priority = direction, priority
        return best_direction
