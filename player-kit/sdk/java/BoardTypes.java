package bot.sdk;

import java.util.Set;

/** Value types shared by the strategy snapshots. */
public final class BoardTypes {
    private BoardTypes() {}

    public record Point(int x, int y) {}

    public record BoostZone(Point marker, Set<Point> cells) {
        public BoostZone {
            cells = Set.copyOf(cells);
        }
    }

    public record Beam(int seat, int x, int y, char dir, int speed, boolean alive,
                       int restartIn) {}
}
