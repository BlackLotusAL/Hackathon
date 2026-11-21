package arena.bot;

import arena.sdk.InitState;
import arena.sdk.TurnState;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;

import static arena.bot.Board.DIRECTIONS;
import static arena.bot.Board.INF;
import static arena.bot.Board.NONE;

final class Planner {
    private static final int[][] SHORT_SIZES = {{4,6}, {5,8}, {6,9}, {5,7}, {3,5}, {3,3}, {2,2}};
    private static final int[][] LONG_SIZES = {
            {3,4}, {4,6}, {5,8}, {6,9}, {5,6}, {7,10}, {3,6}, {2,4}, {3,3}, {2,2}, {8,12}, {10,14}, {12,18}, {14,20}};
    private static final int[] EMPTY = new int[0];
    private static final Candidate NO_ROUTE = new Candidate(EMPTY, -INF, true);

    private record Candidate(int[] moves, double value, boolean risky) {}
    private record Edge(int priority, int distance, int cell) {}

    private final Board board;
    private final boolean search;
    private final boolean unlimited = System.getenv("ARENA_NO_BUDGET") != null;
    private int[] plan = EMPTY;
    private int planIndex;
    private long deadline;

    Planner(InitState init, boolean search) {
        board = new Board(init);
        this.search = search;
    }

    String act(TurnState turn) {
        deadline = System.nanoTime() + (search ? 100_000_000L : 80_000_000L);
        var me = turn.beams().get(board.init.seat());
        if (!me.alive()) {
            clear();
            return String.valueOf(me.dir());
        }
        var view = board.new View(turn);
        var facts = new Facts(view);
        if (!facts.home && facts.threat <= facts.homeDistance + (search ? 1 : 2) && facts.homeDir != NONE) {
            return divert(view, facts, facts.homeDir);
        }
        int target = attack(view, facts);
        if (target == NONE) target = item(view, facts);
        if (target != NONE && safe(view, facts, target)) return divert(view, facts, target);
        if (planIndex == plan.length) {
            if (!facts.home) return divert(view, facts, facts.homeDir == NONE ? facts.dir : facts.homeDir);
            plan = makePlan(view, facts);
            planIndex = 0;
        }
        int d = plan[planIndex];
        if (!safe(view, facts, d)) {
            clear();
            if (!facts.home && facts.homeDir != NONE) return command(facts.homeDir);
            for (int next = 0; next < 4; next++) {
                if (safe(view, facts, next)) return command(next);
            }
            return command(facts.dir);
        }
        if (me.boostLeft() > 0 && planIndex + 1 < plan.length && plan[planIndex + 1] == d
                && (!search || facts.home || facts.homeDistance + 2 < facts.threat)) {
            int second = board.neighbors[board.neighbors[facts.here][d]][d];
            if (board.passable(second) && view.contour[second] != board.own) {
                planIndex += 2;
                return command(d) + "+";
            }
        }
        int adjusted = dodge(view, facts, d);
        if (adjusted != d) {
            clear();
            return command(adjusted);
        }
        planIndex++;
        return command(d);
    }

    private final class Facts {
        final int here;
        final int dir;
        final boolean home;
        final Board.Paths mine;
        final int[] enemy;
        final int enemySpeed;
        int homeDistance;
        int homeDir = NONE;
        int threat = INF;

        Facts(Board.View view) {
            var me = view.turn.beams().get(board.init.seat());
            here = board.cell(me.x(), me.y());
            dir = DIRECTIONS.indexOf(me.dir());
            home = view.owner[here] == board.own;
            mine = board.paths(here, dir ^ 1, view.trail(board.own));
            var opponent = view.turn.beams().get(1 - board.init.seat());
            var core = board.init.calib().get(1 - board.init.seat());
            int start = opponent.alive() ? board.cell(opponent.x(), opponent.y()) : board.cell(core.x(), core.y());
            enemySpeed = opponent.alive() && opponent.boostLeft() > 0 ? 2 : 1;
            enemy = board.paths(start, NONE, null).distance();
            homeDistance = home ? 0 : INF;
            for (int k = 0; k < board.count; k++) {
                if (enemy[k] != INF) enemy[k] = opponent.restartIn() + (enemy[k] + enemySpeed - 1) / enemySpeed;
                if (!home && view.owner[k] == board.own && mine.distance()[k] < homeDistance) {
                    homeDistance = mine.distance()[k];
                    homeDir = mine.first()[k];
                }
                if (view.contour[k] == board.own) threat = Math.min(threat, enemy[k]);
            }
        }
    }

    private boolean overBudget() { return !unlimited && System.nanoTime() - deadline >= 0; }
    private static String command(int d) { return String.valueOf(DIRECTIONS.charAt(d)); }

    private void clear() {
        plan = EMPTY;
        planIndex = 0;
    }

    private boolean safe(Board.View view, Facts f, int d) {
        int k = board.neighbors[f.here][d];
        return d != (f.dir ^ 1) && board.passable(k) && view.contour[k] != board.own;
    }

    private String divert(Board.View view, Facts f, int d) {
        clear();
        return command(dodge(view, f, d));
    }

    private int dodge(Board.View view, Facts f, int d) {
        var enemy = view.turn.beams().get(1 - board.init.seat());
        int target = board.neighbors[f.here][d], from = board.cell(enemy.x(), enemy.y());
        if (!search || !enemy.alive() || target < 0 || view.owner[target] == board.own
                || board.manhattan(from, target) > f.enemySpeed) return d;
        for (int alt = 0; alt < 4; alt++) {
            if (alt == d || !safe(view, f, alt)) continue;
            int k = board.neighbors[f.here][alt];
            if (view.owner[k] == board.own || board.manhattan(from, k) > f.enemySpeed) return alt;
        }
        return d;
    }

    private int attack(Board.View view, Facts f) {
        if (!f.home && f.homeDistance + 1 >= f.threat) return NONE;
        var beam = view.turn.beams().get(1 - board.init.seat());
        if (search && !beam.alive()) return NONE;
        if (search) {
            var paths = board.paths(board.cell(beam.x(), beam.y()), DIRECTIONS.indexOf(beam.dir()) ^ 1,
                    view.trail(board.enemy));
            int back = INF, cut = NONE;
            for (int k = 0; k < board.count; k++) {
                int distance = f.mine.distance()[k];
                if (view.owner[k] == board.enemy && paths.distance()[k] > 0) back = Math.min(back, paths.distance()[k]);
                if (view.contour[k] == board.enemy && distance > 0 && distance <= 20
                        && (cut == NONE || distance < f.mine.distance()[cut])) cut = k;
            }
            if (cut != NONE && f.mine.distance()[cut] <= (back + f.enemySpeed - 1) / f.enemySpeed) {
                return f.mine.first()[cut];
            }
        }
        int best = search ? 13 : 9, target = NONE;
        for (int k = 0; k < board.count; k++) {
            int distance = f.mine.distance()[k];
            boolean latent = view.latent.length > 0 && view.latent[k] != '.' && view.owner[k] == board.enemy;
            if (distance > 0 && distance < best && ((!search && view.contour[k] == board.enemy)
                    || (latent && (!search || distance <= view.latent[k] - '0')))) {
                best = distance;
                target = f.mine.first()[k];
            }
        }
        return target;
    }

    private int item(Board.View view, Facts f) {
        if (!f.home && f.homeDistance + 1 >= f.threat) return NONE;
        int limit = search ? 16 : 12;
        if (view.turn.boost().isPresent()) {
            var boost = view.turn.boost().orElseThrow();
            int k = board.cell(boost.x(), boost.y()), d = f.mine.distance()[k];
            if (d > 0 && d <= limit && (d < f.enemy[k] || (search && d == f.enemy[k] && view.owner[k] == board.own))) {
                return f.mine.first()[k];
            }
        }
        if (search && view.turn.boostNext().isPresent()) {
            var next = view.turn.boostNext().orElseThrow();
            int k = board.cell(next.x(), next.y()), d = f.mine.distance()[k];
            if (d > 0 && d <= limit && d <= f.enemy[k] && view.turn.turn() - 1 + d >= next.at() + 1) {
                return f.mine.first()[k];
            }
        }
        return NONE;
    }

    private double value(Board.View view, int[] captured, int length) {
        var order = view.turn.order().orElse(null);
        boolean finals = view.latent.length > 0;
        boolean inTime = order != null && view.turn.turn() - 1 + length + (finals ? 9 : 1) <= order.until();
        var boost = view.turn.boost().orElse(null);
        var next = view.turn.boostNext().orElse(null);
        int points = 0;
        boolean steals = false;
        for (int k : captured) {
            int x = k % board.size, y = k / board.size;
            int weight = board.value(k);
            points += weight;
            if (view.owner[k] == board.enemy) {
                points += weight;
                steals = true;
            }
            if (inTime && x >= order.rect().x0() && x <= order.rect().x1()
                    && y >= order.rect().y0() && y <= order.rect().y1()) points += weight;
            if (search && (boost != null || next != null)) {
                int bx = boost != null ? boost.x() : next.x(), by = boost != null ? boost.y() : next.y();
                int distance = Math.max(Math.abs(x - bx), Math.abs(y - by));
                points += distance == 0 ? 10 : distance <= 2 ? 4 : 0;
            }
        }
        if (!search) return points * 10 - length;
        double disconnected = finals && steals ? 0.6 * view.disconnectedValue(captured) : 0.0;
        return (points + disconnected) / (length + 8.0);
    }

    private Candidate searchFrom(Board.View view, Facts f, int start, int dir, int[] prefix) {
        Candidate best = NO_ROUTE;
        for (int first = 0; first < 4; first++) {
            if (first == (dir ^ 1)) continue;
            int exit = start;
            int inner = 0;
            while (inner < 30) {
                int next = board.neighbors[exit][first];
                if (!board.passable(next) || view.owner[next] != board.own) break;
                exit = next;
                inner++;
            }
            for (int second = first < 2 ? 2 : 0; second < (first < 2 ? 4 : 2); second++) {
                for (int[] size : search ? LONG_SIZES : SHORT_SIZES) {
                    if (overBudget()) return best;
                    Candidate candidate = candidate(view, f, exit, first, second, size, prefix, inner);
                    if (candidate.value > best.value) best = candidate;
                }
            }
        }
        return best;
    }

    private Candidate candidate(Board.View view, Facts f, int exit, int first, int second,
                                int[] size, int[] prefix, int inner) {
        int a = size[0], legs = a + size[1], k = exit;
        boolean[] blocked = view.trail(board.own), trail = new boolean[board.count];
        for (int step = 0; step < legs; step++) {
            int next = board.neighbors[k][step < a ? first : second];
            if (!board.passable(next) || blocked[next] || view.owner[next] == board.own) return NO_ROUTE;
            k = next;
            blocked[k] = trail[k] = true;
        }
        var paths = board.paths(k, second ^ 1, blocked);
        int home = NONE, back = legs + (search ? 12 : 6) + 1;
        for (int tile = 0; tile < board.count; tile++) {
            if (view.owner[tile] == board.own && paths.distance()[tile] < back) {
                home = tile;
                back = paths.distance()[tile];
            }
        }
        if (home == NONE) return NO_ROUTE;
        int[] route = paths.route(home);
        for (int d : route) {
            k = board.neighbors[k][d];
            trail[k] = view.owner[k] != board.own;
        }
        int length = prefix.length + inner + legs + route.length;
        boolean risky = false;
        for (int tile = 0; tile < board.count; tile++) {
            if (trail[tile] && f.enemy[tile] <= length) {
                risky = true;
                break;
            }
        }
        if (risky && length > 40) return NO_ROUTE;
        int[] captured = view.captured(trail);
        if (captured.length == 0) return NO_ROUTE;
        int[] moves = Arrays.copyOf(prefix, length);
        int endOfFirst = prefix.length + inner + a;
        Arrays.fill(moves, prefix.length, endOfFirst, first);
        Arrays.fill(moves, endOfFirst, length - route.length, second);
        System.arraycopy(route, 0, moves, length - route.length, route.length);
        return new Candidate(moves, value(view, captured, length), risky);
    }

    private int[] makePlan(Board.View view, Facts f) {
        Candidate best = searchFrom(view, f, f.here, f.dir, EMPTY);
        if (best.moves.length > 0 && !best.risky) return best.moves;
        var edges = new ArrayList<Edge>();
        for (int k = 0; k < board.count; k++) {
            int d = f.mine.distance()[k];
            if (view.owner[k] == board.own && d > 0 && d < INF && view.edge(k)) edges.add(new Edge(d - f.enemy[k], d, k));
        }
        var nearest = Comparator.comparingInt(Edge::distance).thenComparingInt(Edge::cell);
        edges.sort(Comparator.comparingInt(Edge::priority).thenComparing(nearest));
        var used = new ArrayList<Integer>();
        for (var edge : edges) {
            if (overBudget() || used.size() >= (search ? 4 : 3)) break;
            int k = edge.cell();
            if (edge.distance() > 20 || used.stream().anyMatch(old -> board.manhattan(k, old) < 6)) continue;
            int[] prefix = f.mine.route(k);
            used.add(k);
            Candidate candidate = searchFrom(view, f, k, prefix[prefix.length - 1], prefix);
            if (!candidate.risky && (best.risky || candidate.value > best.value)) best = candidate;
        }
        if (best.moves.length > 0) return best.moves;
        return edges.stream().min(nearest).map(edge -> f.mine.route(edge.cell())).orElseGet(() -> new int[]{f.dir});
    }
}
