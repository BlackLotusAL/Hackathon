package bot;

import bot.sdk.InitState;
import bot.sdk.TurnState;
import java.util.Arrays;

final class Board {
    static final int INF = 1_000_000;
    static final int NONE = -1;
    static final String DIRECTIONS = "UDLR";
    private static final int[] DX = {0, 0, -1, 1};
    private static final int[] DY = {-1, 1, 0, 0};

    final InitState init;
    final int size;
    final int count;
    final char own;
    final char enemy;
    final String terrain;
    final int[][] neighbors;
    final boolean[] protectedCells;

    Board(InitState init) {
        this.init = init;
        size = init.size();
        count = size * size;
        own = (char) ('0' + init.seat());
        enemy = (char) ('0' + 1 - init.seat());
        terrain = String.join("", init.terrain());
        neighbors = new int[count][4];
        protectedCells = new boolean[count];
        for (int k = 0; k < count; k++) {
            for (int d = 0; d < 4; d++) {
                int x = k % size + DX[d], y = k / size + DY[d];
                neighbors[k][d] = x >= 0 && y >= 0 && x < size && y < size ? cell(x, y) : NONE;
            }
        }
        for (var center : init.calib()) {
            for (int y = center.y() - 1; y <= center.y() + 1; y++) {
                for (int x = center.x() - 1; x <= center.x() + 1; x++) protectedCells[cell(x, y)] = true;
            }
        }
    }

    record Paths(int start, int[] distance, int[] parent, int[] incoming, int[] first) {
        int[] route(int target) {
            if (distance[target] == INF) return new int[0];
            int[] result = new int[distance[target]];
            for (int i = result.length - 1, k = target; i >= 0; i--) {
                result[i] = incoming[k];
                k = parent[k];
            }
            return result;
        }
    }

    int cell(int x, int y) { return y * size + x; }
    int value(int k) { return terrain.charAt(k) == '*' ? 3 : 1; }
    int manhattan(int a, int b) { return Math.abs(a % size - b % size) + Math.abs(a / size - b / size); }
    boolean passable(int k) { return k >= 0 && terrain.charAt(k) != '#'; }

    Paths paths(int start, int banned, boolean[] blocked) {
        int[] distance = new int[count], parent = new int[count], incoming = new int[count], first = new int[count];
        Arrays.fill(distance, INF);
        Arrays.fill(first, NONE);
        int[] queue = new int[count];
        queue[0] = start;
        distance[start] = 0;
        for (int head = 0, tail = 1; head < tail; head++) {
            int k = queue[head];
            for (int d = 0; d < 4; d++) {
                int next = neighbors[k][d];
                if ((k == start && d == banned) || !passable(next)
                        || distance[next] != INF || (blocked != null && blocked[next])) continue;
                distance[next] = distance[k] + 1;
                parent[next] = k;
                incoming[next] = d;
                first[next] = k == start ? d : first[k];
                queue[tail++] = next;
            }
        }
        return new Paths(start, distance, parent, incoming, first);
    }

    final class View {
        final TurnState turn;
        final char[] owner;
        final char[] contour;
        final char[] latent;

        View(TurnState turn) {
            this.turn = turn;
            owner = String.join("", turn.owner()).toCharArray();
            contour = String.join("", turn.contour()).toCharArray();
            latent = String.join("", turn.latent()).toCharArray();
        }

        boolean edge(int k) {
            for (int next : neighbors[k]) {
                if (passable(next) && owner[next] != own) return true;
            }
            return false;
        }

        boolean[] trail(char seat) {
            boolean[] blocked = new boolean[count];
            for (int k = 0; k < count; k++) blocked[k] = contour[k] == seat;
            return blocked;
        }

        int[] captured(boolean[] route) {
            boolean[] wall = trail(own), seen = new boolean[count];
            int[] queue = new int[count];
            int tail = 0;
            for (int k = 0; k < count; k++) {
                wall[k] = wall[k] || owner[k] == own || route[k];
                if (wall[k]) continue;
                int x = k % size, y = k / size;
                if (x == 0 || y == 0 || x == size - 1 || y == size - 1) {
                    seen[k] = true;
                    queue[tail++] = k;
                }
            }
            for (int head = 0; head < tail; head++) {
                for (int next : neighbors[queue[head]]) {
                    if (next >= 0 && !wall[next] && !seen[next]) {
                        seen[next] = true;
                        queue[tail++] = next;
                    }
                }
            }
            int length = 0;
            for (int k = 0; k < count; k++) {
                if (owner[k] != own && !protectedCells[k] && passable(k)
                        && (route[k] || contour[k] == own || !seen[k])) queue[length++] = k;
            }
            return Arrays.copyOf(queue, length);
        }

        int disconnectedValue(int[] captured) {
            boolean[] blocked = new boolean[count], seen = new boolean[count];
            for (int k : captured) blocked[k] = true;
            var core = init.calib().get(1 - init.seat());
            int start = cell(core.x(), core.y());
            int[] queue = new int[count];
            queue[0] = start;
            seen[start] = true;
            for (int head = 0, tail = 1; head < tail; head++) {
                for (int next : neighbors[queue[head]]) {
                    if (next >= 0 && owner[next] == enemy && !blocked[next] && !seen[next]) {
                        seen[next] = true;
                        queue[tail++] = next;
                    }
                }
            }
            int result = 0;
            for (int k = 0; k < count; k++) {
                if (owner[k] == enemy && latent[k] == '.' && !blocked[k] && !seen[k]) result += value(k);
            }
            return result;
        }
    }
}
