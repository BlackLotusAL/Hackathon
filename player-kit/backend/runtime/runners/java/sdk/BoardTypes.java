package arena.sdk;

/** Value types shared by the strategy snapshots. */
public final class BoardTypes {
    private BoardTypes() {}

    public record Point(int x, int y) {}

    public record Rect(int x0, int y0, int x1, int y1) {}

    public record Beam(int seat, int x, int y, char dir, int speed, boolean alive,
                       int restartIn, int boostLeft) {}

    public record Order(int no, Rect rect, int until) {}

    public record NextBoost(int x, int y, int at) {}
}
