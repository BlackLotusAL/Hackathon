#pragma once

#include "bot_sdk.hpp"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <string>
#include <vector>

namespace baseline {

constexpr int unreachable = 1'000'000;
constexpr int no_direction = -1;
constexpr char directions[] = "UDLR";
constexpr int dx[] = {0, 0, -1, 1};
constexpr int dy[] = {-1, 1, 0, 0};
using Route = std::vector<int>;
using Mask = std::vector<unsigned char>;

struct Paths {
    int start;
    std::vector<int> distance;
    std::vector<int> parent;
    std::vector<int> incoming;
    std::vector<int> first;

    Route route(int target) const {
        if (distance[target] <= 0) return {};
        Route result;
        for (int k = target; k != start; k = parent[k]) result.push_back(incoming[k]);
        std::reverse(result.begin(), result.end());
        return result;
    }
};

struct Board {
    const bot_sdk::InitState& init;
    const bot_sdk::TurnState& turn;
    int size;
    int count;
    char own;
    char enemy;
    const std::string& terrain;
    std::string owner;
    std::string contour;
    const std::vector<std::array<int, 4>>& neighbors;
    const Mask& protected_cells;

    Board(const bot_sdk::InitState& init, const bot_sdk::TurnState& turn,
          const std::vector<std::array<int, 4>>& neighbors, const std::string& terrain,
          const Mask& protected_cells)
        : init(init), turn(turn), size(init.map.size()), count(size * size), own('0' + init.seat),
          enemy('0' + (1 - init.seat)), terrain(terrain), owner(flatten(turn.owner)),
          contour(flatten(turn.contour)),
          neighbors(neighbors), protected_cells(protected_cells) {}

    static std::string flatten(const std::vector<std::string>& rows) {
        std::string result;
        for (const auto& row : rows) result += row;
        return result;
    }

    int cell(int x, int y) const { return y * size + x; }
    int value(int k) const { return terrain[k] == '*' ? 3 : 1; }
    int distance(int a, int b) const { return std::abs(a % size - b % size) + std::abs(a / size - b / size); }
    bool passable(int k) const { return k >= 0 && terrain[k] != '#'; }
    int score_gain(const std::vector<int>& captured) const {
        std::string projected = owner;
        for (int k : captured) projected[k] = own;
        std::array<int, 2> face{}, edges{};
        for (int k = 0; k < count; ++k) {
            if (projected[k] == '.') continue;
            const int seat = projected[k] - '0';
            face[seat] += value(k);
            if (!protected_cells[k]) {
                for (int n : neighbors[k]) {
                    if (n < 0 || projected[n] != projected[k]) ++edges[seat];
                }
            }
        }
        const int seat = init.seat, other = 1 - seat;
        return (face[seat] - edges[seat] / 3 - turn.score[seat])
             - (face[other] - edges[other] / 3 - turn.score[other]);
    }

    int enemy_steps() const {
        const auto& beam = turn.beams[1 - init.seat];
        if (!beam.alive) return 1;
        return init.map.move_steps(beam.x, beam.y);
    }

    bool edge(int k) const {
        for (int next : neighbors[k]) {
            if (passable(next) && owner[next] != own) return true;
        }
        return false;
    }

    Paths paths(int start, int banned = no_direction, const Mask* blocked = nullptr) const {
        Paths result{start, std::vector<int>(count, unreachable), std::vector<int>(count, -1),
                     std::vector<int>(count, no_direction), std::vector<int>(count, no_direction)};
        std::vector<int> queue{start};
        queue.reserve(count);
        result.distance[start] = 0;
        for (std::size_t i = 0; i < queue.size(); ++i) {
            const int k = queue[i];
            for (int d = 0; d < 4; ++d) {
                const int next = neighbors[k][d];
                if ((k == start && d == banned) || !passable(next)
                        || result.distance[next] != unreachable || (blocked && (*blocked)[next])) continue;
                result.distance[next] = result.distance[k] + 1;
                result.parent[next] = k;
                result.incoming[next] = d;
                result.first[next] = k == start ? d : result.first[k];
                queue.push_back(next);
            }
        }
        return result;
    }

    Mask trail(char seat) const {
        Mask blocked(count);
        for (int k = 0; k < count; ++k) blocked[k] = contour[k] == seat;
        return blocked;
    }

    std::vector<int> captured(const Mask& route) const {
        auto wall = trail(own);
        Mask seen(count);
        std::vector<int> queue;
        queue.reserve(count);
        for (int k = 0; k < count; ++k) {
            wall[k] = wall[k] || owner[k] == own || route[k];
            const auto& adj = neighbors[k];
            if (!wall[k] && std::find(adj.begin(), adj.end(), -1) != adj.end()) {
                seen[k] = 1;
                queue.push_back(k);
            }
        }
        for (std::size_t i = 0; i < queue.size(); ++i) {
            for (int next : neighbors[queue[i]]) {
                if (next >= 0 && !wall[next] && !seen[next]) {
                    seen[next] = 1;
                    queue.push_back(next);
                }
            }
        }
        std::vector<int> result;
        for (int k = 0; k < count; ++k) {
            if (owner[k] != own && !protected_cells[k] && passable(k)
                    && (route[k] || contour[k] == own || !seen[k])) result.push_back(k);
        }
        return result;
    }

};

}  // namespace baseline
