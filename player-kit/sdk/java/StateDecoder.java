package bot;

import bot.sdk.BoardTypes.Beam;
import bot.sdk.BoardTypes.Point;
import bot.sdk.InitState;
import bot.sdk.TurnState;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

final class StateDecoder {
    private StateDecoder() {}

    static final class InvalidState extends RuntimeException {}

    static InitState init(JsonNode msg) {
        int seat = integer(msg.path("seat"));
        if (seat < 0 || seat > 1 || !"prelim".equals(msg.path("stage").textValue())) {
            throw new InvalidState();
        }
        return new InitState(seat, integer(msg.path("size")), integer(msg.path("max_turns")),
                text(msg.path("stage")), text(msg.path("map")),
                list(msg.path("terrain"), StateDecoder::text),
                pair(msg.path("calib"), StateDecoder::coordinates),
                list(msg.path("boost_points"), StateDecoder::coordinates));
    }

    static TurnState turn(JsonNode msg) {
        return new TurnState(integer(msg.path("turn")),
                list(msg.path("owner"), StateDecoder::text), list(msg.path("contour"), StateDecoder::text),
                pair(msg.path("beams"), StateDecoder::beam), pair(msg.path("score"), StateDecoder::integer),
                pair(msg.path("lead_turns"), StateDecoder::integer), pair(msg.path("time_us"), StateDecoder::integer));
    }

    static int integer(JsonNode value) {
        if (!value.isIntegralNumber() || !value.canConvertToInt()) {
            throw new InvalidState();
        }
        return value.intValue();
    }

    private static String text(JsonNode value) {
        if (!value.isTextual()) {
            throw new InvalidState();
        }
        return value.textValue();
    }

    private static Point coordinates(JsonNode value) {
        List<Integer> xy = pair(value, StateDecoder::integer);
        return new Point(xy.get(0), xy.get(1));
    }

    private static Beam beam(JsonNode value) {
        String dir = text(value.path("dir"));
        JsonNode alive = value.path("alive");
        if (dir.length() != 1 || "UDLR".indexOf(dir.charAt(0)) < 0 || !alive.isBoolean()) {
            throw new InvalidState();
        }
        return new Beam(integer(value.path("seat")), integer(value.path("x")), integer(value.path("y")),
                dir.charAt(0), integer(value.path("speed")), alive.booleanValue(),
                integer(value.path("restart_in")));
    }

    private static <T> List<T> list(JsonNode value, Function<JsonNode, T> decode) {
        if (!value.isArray()) {
            throw new InvalidState();
        }
        List<T> result = new ArrayList<>(value.size());
        for (JsonNode item : value) {
            result.add(decode.apply(item));
        }
        return result;
    }

    private static <T> List<T> pair(JsonNode value, Function<JsonNode, T> decode) {
        if (!value.isArray() || value.size() != 2) {
            throw new InvalidState();
        }
        return List.of(decode.apply(value.get(0)), decode.apply(value.get(1)));
    }
}
