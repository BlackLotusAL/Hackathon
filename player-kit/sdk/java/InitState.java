package bot.sdk;

import bot.sdk.BoardTypes.Point;
import java.util.List;
import java.util.Optional;

/** The seat and map data supplied once when the match opens. */
public record InitState(int seat, int size, int maxTurns, String stage, String map,
                        List<String> terrain, List<Point> calib, List<Point> boostPoints) {
    public InitState {
        terrain = List.copyOf(terrain);
        calib = List.copyOf(calib);
        boostPoints = List.copyOf(boostPoints);
    }

    public Optional<Point> activeStation(int turn) {
        if (turn < 1 || boostPoints.isEmpty()) return Optional.empty();
        return Optional.of(boostPoints.get(((turn - 1) / 100) % boostPoints.size()));
    }

    public int moveSteps(int turn, int x, int y) {
        var station = activeStation(turn).orElse(null);
        return station != null && Math.abs(x - station.x()) <= 1 && Math.abs(y - station.y()) <= 1 ? 2 : 1;
    }
}
