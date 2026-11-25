package bot;

import bot.sdk.InitState;
import bot.sdk.TurnState;

public final class Strategy {
    private static final String DIRECTIONS = "UDLR";
    private static final int[] DX = {0, 0, -1, 1};
    private static final int[] DY = {-1, 1, 0, 0};
    private final InitState init;

    public Strategy(InitState init) {
        this.init = init;
    }

    public String act(TurnState state) {
        var me = state.beams().get(init.seat());
        if (!me.alive()) {
            return String.valueOf(me.dir());
        }

        char own = (char) ('0' + init.seat());
        int forward = DIRECTIONS.indexOf(me.dir());
        char bestDirection = me.dir();
        int bestPriority = Integer.MIN_VALUE;
        for (int offset = 0; offset < DIRECTIONS.length(); offset++) {
            int d = (forward + offset) % DIRECTIONS.length();
            if (d == (forward ^ 1)) {
                continue;
            }
            int x = me.x(), y = me.y();
            boolean safe = true;
            int steps = init.moveSteps(state.turn(), x, y);
            for (int i = 0; i < steps; i++) {
                x += DX[d];
                y += DY[d];
                if (x < 0 || x >= init.size() || y < 0 || y >= init.size()
                        || init.terrain().get(y).charAt(x) == '#' || state.contour().get(y).charAt(x) == '0' + init.seat()) {
                    safe = false;
                    break;
                }
            }
            if (!safe) continue;
            int priority = state.owner().get(y).charAt(x) == own ? 0 : 1;
            if (priority > 0) {
                if (init.terrain().get(y).charAt(x) == '*') priority += 2;
                for (int i = 0; i < 4; i++) {
                    int nx = x + DX[i], ny = y + DY[i];
                    if (nx >= 0 && nx < init.size() && ny >= 0 && ny < init.size()
                            && state.owner().get(ny).charAt(nx) == own) priority++;
                }
            }
            if (priority > bestPriority) {
                bestDirection = DIRECTIONS.charAt(d);
                bestPriority = priority;
            }
        }
        return String.valueOf(bestDirection);
    }
}
