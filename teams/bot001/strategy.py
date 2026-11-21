"""bot001 随机：每回合在不撞缺陷、不出界、不扫过自己轮廓、不掉头的方向里随机选一个。策略说明见 README.md。

随机数种子由地图编号和座位决定，同一局可以复现；设置环境变量 ARENA_SEED 可以换种子。
"""
import os
import random

from game_core.rules import DIRS, DX, DY, OPP
from backend.runtime.state import InitState, TurnState


class Strategy:
    def __init__(self, init: InitState):
        self.init = init
        self.rng = random.Random('%s-%s-%s' % (init.map, init.seat, os.environ.get('ARENA_SEED', '0')))

    def act(self, state: TurnState) -> str:
        s = self.init.seat
        me = state.beams[s]
        if not me.alive:
            return me.dir
        ok = []
        for d in DIRS:
            if d == OPP[me.dir]:
                continue
            x, y = me.x + DX[d], me.y + DY[d]
            if (0 <= x < self.init.size and 0 <= y < self.init.size
                    and self.init.terrain[y][x] != '#' and state.contour[y][x] != str(s)):
                ok.append(d)
        return self.rng.choice(ok) if ok else me.dir
