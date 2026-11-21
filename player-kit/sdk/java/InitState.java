package bot.sdk;

import bot.sdk.BoardTypes.Point;
import bot.sdk.BoardTypes.Rect;
import java.util.List;

/** The seat and map data supplied once when the match opens. */
public record InitState(int seat, int size, int maxTurns, String stage, String map,
                        List<String> terrain, List<Point> calib, List<Rect> orders,
                        List<Point> boostPoints) {
    public InitState {
        terrain = List.copyOf(terrain);
        calib = List.copyOf(calib);
        orders = List.copyOf(orders);
        boostPoints = List.copyOf(boostPoints);
    }
}
