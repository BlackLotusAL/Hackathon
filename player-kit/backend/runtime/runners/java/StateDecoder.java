package arena.bot;

import arena.sdk.BoardTypes.Beam;
import arena.sdk.BoardTypes.NextBoost;
import arena.sdk.BoardTypes.Order;
import arena.sdk.BoardTypes.Point;
import arena.sdk.BoardTypes.Rect;
import arena.sdk.InitState;
import arena.sdk.TurnState;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.function.Function;

final class StateDecoder {
    private StateDecoder() {}

    static final class InvalidState extends RuntimeException {}

    static InitState init(JsonNode msg) {
        int seat = integer(msg.path("seat"));
        if (seat < 0 || seat > 1) {
            throw new InvalidState();
        }
        return new InitState(seat, integer(msg.path("size")), integer(msg.path("max_turns")),
                text(msg.path("stage")), text(msg.path("map")),
                list(msg.path("terrain"), StateDecoder::text),
                pair(msg.path("calib"), StateDecoder::coordinates),
                list(msg.path("orders"), StateDecoder::rect),
                list(msg.path("boost_points"), StateDecoder::coordinates));
    }

    static TurnState turn(JsonNode msg) {
        return new TurnState(integer(msg.path("turn")),
                list(msg.path("owner"), StateDecoder::text), list(msg.path("contour"), StateDecoder::text),
                optional(msg, "latent", value -> list(value, StateDecoder::text)).orElseGet(List::of),
                pair(msg.path("beams"), StateDecoder::beam),
                pair(msg.path("score"), StateDecoder::integer), pair(msg.path("bonus"), StateDecoder::integer),
                optional(msg, "order", value -> new Order(integer(value.path("no")),
                        rect(value.path("rect")), integer(value.path("until")))),
                optional(msg, "boost", StateDecoder::point),
                optional(msg, "boost_next", value -> new NextBoost(integer(value.path("x")),
                        integer(value.path("y")), integer(value.path("at")))));
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

    private static Point point(JsonNode value) {
        return new Point(integer(value.path("x")), integer(value.path("y")));
    }

    private static Point coordinates(JsonNode value) {
        List<Integer> xy = pair(value, StateDecoder::integer);
        return new Point(xy.get(0), xy.get(1));
    }

    private static Rect rect(JsonNode value) {
        if (!value.isArray() || value.size() != 4) {
            throw new InvalidState();
        }
        return new Rect(integer(value.get(0)), integer(value.get(1)), integer(value.get(2)), integer(value.get(3)));
    }

    private static Beam beam(JsonNode value) {
        String dir = text(value.path("dir"));
        JsonNode alive = value.path("alive");
        if (dir.length() != 1 || "UDLR".indexOf(dir.charAt(0)) < 0 || !alive.isBoolean()) {
            throw new InvalidState();
        }
        return new Beam(integer(value.path("seat")), integer(value.path("x")), integer(value.path("y")),
                dir.charAt(0), integer(value.path("speed")), alive.booleanValue(),
                integer(value.path("restart_in")), integer(value.path("boost_left")));
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

    private static <T> Optional<T> optional(JsonNode parent, String key, Function<JsonNode, T> decode) {
        JsonNode value = parent.get(key);
        return value == null || value.isNull() ? Optional.empty() : Optional.of(decode.apply(value));
    }
}
