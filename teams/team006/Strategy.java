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

        var order = state.order().orElse(null);
        char own = (char) ('0' + init.seat());
        int forward = DIRECTIONS.indexOf(me.dir());
        char bestDirection = me.dir();
        int bestPriority = Integer.MIN_VALUE;
        for (int offset = 0; offset < DIRECTIONS.length(); offset++) {
            int d = (forward + offset) % DIRECTIONS.length();
            if (d == (forward ^ 1)) {
                continue;
            }
            int x = me.x() + DX[d];
            int y = me.y() + DY[d];
            if (x < 0 || x >= init.size() || y < 0 || y >= init.size()
                    || init.terrain().get(y).charAt(x) == '#'
                    || state.contour().get(y).charAt(x) == own) {
                continue;
            }

            int priority = state.owner().get(y).charAt(x) == own ? 0 : 1;
            if (order != null) {
                var rect = order.rect();
                int distanceX = Math.max(0, Math.max(rect.x0() - x, x - rect.x1()));
                int distanceY = Math.max(0, Math.max(rect.y0() - y, y - rect.y1()));
                priority -= 2 * (distanceX + distanceY);
            }
            if (priority > bestPriority) {
                bestDirection = DIRECTIONS.charAt(d);
                bestPriority = priority;
            }
        }
        return String.valueOf(bestDirection);
    }
}
