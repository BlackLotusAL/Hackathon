package bot.sdk;

/** Value types shared by the strategy snapshots. */
public final class BoardTypes {
    private BoardTypes() {}

    public record Point(int x, int y) {}

    public record Beam(int seat, int x, int y, char dir, int speed, boolean alive,
                       int restartIn) {}
}
