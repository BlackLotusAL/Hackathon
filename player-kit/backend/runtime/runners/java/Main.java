package arena.bot;

import arena.sdk.InitState;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.locks.ReentrantLock;
import java.util.regex.Pattern;

public final class Main {
    private static final int MAX_REQUEST_BYTES = 65536;
    private static final int MAX_TURNS = 600;
    private static final Pattern MATCH_PATH =
            Pattern.compile("/v1/matches/([A-Za-z0-9_.-]{1,64})/(init|end|turns/([1-9][0-9]{0,2}))");
    private static final Pattern COMMAND = Pattern.compile("[UDLR]\\+?");
    private static final ObjectMapper JSON = new ObjectMapper().enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);

    private static final class Match {
        final ReentrantLock lock = new ReentrantLock();
        Strategy strategy;
        int newestTurn;
    }

    private record Response(int status, Map<String, ?> body) {
        Response(int status) {
            this(status, null);
        }
    }

    private final Map<String, Match> matches = new HashMap<>();
    private final String token;

    private Main(String token) {
        this.token = token;
    }

    public static void main(String[] args) throws IOException {
        var env = System.getenv();
        String host = env.getOrDefault("BOT_HOST", "0.0.0.0");
        int port = Integer.parseInt(env.getOrDefault("BOT_PORT", "8080"));
        var server = HttpServer.create(new InetSocketAddress(host, port), 0);
        var service = new Main(env.getOrDefault("BOT_TOKEN", ""));
        server.createContext("/", service::handle);
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        System.err.printf("[sdk] listening on %s:%d%n", host, server.getAddress().getPort());
    }

    private void handle(HttpExchange exchange) throws IOException {
        try (exchange) {
            Response response;
            try {
                response = route(exchange);
            } catch (JsonProcessingException | CharacterCodingException | StateDecoder.InvalidState error) {
                response = new Response(400);
            } catch (RuntimeException error) {
                error.printStackTrace(System.err);
                response = new Response(500);
            }
            exchange.getResponseHeaders().set("Connection", "close");
            if (response.body() == null) {
                exchange.sendResponseHeaders(response.status(), -1);
            } else {
                byte[] json = JSON.writeValueAsBytes(response.body());
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(response.status(), json.length);
                exchange.getResponseBody().write(json);
            }
        }
    }

    private Response route(HttpExchange exchange) throws IOException {
        var headers = exchange.getRequestHeaders();
        if (!token.isEmpty() && !token.equals(headers.getFirst("X-Arena-Token"))) {
            return new Response(401);
        }
        if (!"1".equals(headers.getFirst("X-Arena-Protocol"))) {
            return new Response(400);
        }

        var uri = exchange.getRequestURI();
        String path = uri.getRawPath();
        if (uri.getRawQuery() != null) {
            return new Response(404);
        }
        if ("GET".equals(exchange.getRequestMethod()) && "/v1/health".equals(path)) {
            return new Response(200, Map.of("ready", true, "name", "java", "sdk", "java/1"));
        }
        var route = MATCH_PATH.matcher(path);
        if (!"POST".equals(exchange.getRequestMethod()) || !route.matches()) {
            return new Response(404);
        }

        int size;
        try {
            String length = headers.getFirst("Content-Length");
            size = length == null ? 0 : Integer.parseInt(length);
        } catch (NumberFormatException error) {
            return new Response(400);
        }
        if (size <= 0 || size > MAX_REQUEST_BYTES) {
            return new Response(413);
        }
        byte[] raw = exchange.getRequestBody().readNBytes(size);
        if (raw.length != size) {
            return new Response(400);
        }
        String body = StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(raw)).toString();
        JsonNode message = JSON.readTree(body);
        String id = route.group(1);
        String turnPart = route.group(3);
        String kind = turnPart == null ? route.group(2) : "turn";
        if (message == null || !message.isObject() || !kind.equals(message.path("type").textValue())) {
            return new Response(400);
        }
        return switch (kind) {
            case "init" -> initialize(id, StateDecoder.init(message));
            case "end" -> finish(id);
            default -> {
                int turn = Integer.parseInt(turnPart);
                if (turn > MAX_TURNS || turn != StateDecoder.integer(message.path("turn"))) {
                    yield new Response(400);
                }
                yield playTurn(id, turn, message);
            }
        };
    }

    private Response initialize(String id, InitState state) {
        var match = new Match();
        match.lock.lock();
        try {
            synchronized (matches) {
                if (matches.putIfAbsent(id, match) != null) {
                    return new Response(409);
                }
            }
            try {
                match.strategy = new Strategy(state);
            } catch (RuntimeException error) {
                synchronized (matches) {
                    matches.remove(id, match);
                }
                throw error;
            }
            return new Response(204);
        } finally {
            match.lock.unlock();
        }
    }

    private Response playTurn(String id, int turn, JsonNode message) {
        Match match;
        synchronized (matches) {
            match = matches.get(id);
            if (match == null) {
                return new Response(404);
            }
            if (turn <= match.newestTurn) {
                return new Response(409);
            }
            match.newestTurn = turn;
        }
        match.lock.lock();
        try {
            synchronized (matches) {
                // A newer request may have superseded this turn while the strategy was busy.
                if (matches.get(id) != match || turn < match.newestTurn) {
                    return new Response(409);
                }
            }
            String command = match.strategy.act(StateDecoder.turn(message));
            if (command == null || !COMMAND.matcher(command).matches()) {
                return new Response(500);
            }
            return new Response(200, Map.of("turn", turn, "cmd", command));
        } finally {
            match.lock.unlock();
        }
    }

    private Response finish(String id) {
        synchronized (matches) {
            return new Response(matches.remove(id) == null ? 404 : 204);
        }
    }
}
