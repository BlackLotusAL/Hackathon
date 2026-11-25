package bot.sdk;

import bot.sdk.BoardTypes.BoostZone;
import bot.sdk.BoardTypes.Point;
import java.util.List;

/** The static map shared by all turns of a match. */
public record GameMap(String id, String stage, int size, List<String> terrain,
                      List<Point> calib, List<BoostZone> boostZones) {
    public GameMap {
        terrain = List.copyOf(terrain);
        calib = List.copyOf(calib);
        boostZones = List.copyOf(boostZones);
    }

    public int moveSteps(int x, int y) {
        var point = new Point(x, y);
        for (var zone : boostZones) {
            if (zone.cells().contains(point)) return 2;
        }
        return 1;
    }
}
