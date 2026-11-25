package bot.sdk;

/** The match settings and static map supplied when the match opens. */
public record InitState(int seat, int maxTurns, GameMap map) {}
