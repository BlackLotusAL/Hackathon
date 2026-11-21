package arena.bot;

import arena.sdk.InitState;
import arena.sdk.TurnState;

public final class Strategy {
    private final Planner planner;

    public Strategy(InitState init) {
        planner = new Planner(init, true);
    }

    public String act(TurnState state) {
        return planner.act(state);
    }
}
