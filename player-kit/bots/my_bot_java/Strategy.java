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
        int forward = DIRECTIONS.indexOf(me.dir());

        if (me.alive()) {
            for (int offset = 0; offset < DIRECTIONS.length(); offset++) {
                int d = (forward + offset) % DIRECTIONS.length();
                if (d == (forward ^ 1)) {
                    continue;
                }
                int x = me.x() + DX[d];
                int y = me.y() + DY[d];
                if (x >= 0 && x < init.size() && y >= 0 && y < init.size()
                        && init.terrain().get(y).charAt(x) != '#'
                        && state.contour().get(y).charAt(x) != '0' + init.seat()) {
                    return String.valueOf(DIRECTIONS.charAt(d));
                }
            }
        }
        return String.valueOf(me.dir());
    }
}
