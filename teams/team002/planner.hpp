#pragma once

#include "board.hpp"

#include <chrono>
#include <string_view>
#include <tuple>
#include <utility>

namespace baseline {

class Planner {
public:
    Planner(const bot_sdk::InitState& init, bool search)
        : init_(init), search_(search), unlimited_(std::getenv("ARENA_NO_BUDGET") != nullptr),
          terrain_(Board::flatten(init.terrain)), neighbors_(init.size * init.size),
          protected_(init.size * init.size) {
        const int n = init.size;
        for (int y = 0; y < n; ++y) {
            for (int x = 0; x < n; ++x) {
                auto& adj = neighbors_[y * n + x];
                for (int d = 0; d < 4; ++d) {
                    const int a = x + dx[d], b = y + dy[d];
                    adj[d] = a >= 0 && b >= 0 && a < n && b < n ? b * n + a : -1;
                }
            }
        }
        for (const auto& center : init.calib) {
            for (int y = center.y - 1; y <= center.y + 1; ++y) {
                for (int x = center.x - 1; x <= center.x + 1; ++x) protected_[y * n + x] = 1;
            }
        }
    }

    std::string act(const bot_sdk::TurnState& turn) {
        deadline_ = Clock::now() + std::chrono::milliseconds(search_ ? 100 : 80);
        const auto& me = turn.beams[init_.seat];
        if (!me.alive) {
            plan_.clear();
            return std::string(1, me.dir);
        }
        const Board board(init_, turn, neighbors_, terrain_, protected_);
        const Facts facts(board);
        if (!facts.home && facts.threat <= facts.home_distance + (search_ ? 1 : 2)
                && facts.home_dir != no_direction) return divert(board, facts, facts.home_dir);
        int target = attack(board, facts);
        if (target == no_direction) target = item(board, facts);
        if (target != no_direction && safe(board, facts, target)) return divert(board, facts, target);
        if (plan_.empty()) {
            if (!facts.home) return divert(board, facts, facts.home_dir == no_direction ? facts.dir : facts.home_dir);
            plan_ = make_plan(board, facts);
        }
        const int d = plan_.front();
        if (!safe(board, facts, d)) {
            plan_.clear();
            if (!facts.home && facts.home_dir != no_direction) return command(facts.home_dir);
            for (int next = 0; next < 4; ++next) {
                if (safe(board, facts, next)) return command(next);
            }
            return command(facts.dir);
        }
        if (me.boost_left > 0 && plan_.size() >= 2 && plan_[1] == d
                && (!search_ || facts.home || facts.home_distance + 2 < facts.threat)) {
            const int second = board.neighbors[board.neighbors[facts.here][d]][d];
            if (board.passable(second) && board.contour[second] != board.own) {
                plan_.erase(plan_.begin(), plan_.begin() + 2);
                return command(d) + '+';
            }
        }
        const int adjusted = dodge(board, facts, d);
        if (adjusted != d) {
            plan_.clear();
            return command(adjusted);
        }
        plan_.erase(plan_.begin());
        return command(d);
    }

private:
    using Clock = std::chrono::steady_clock;
    struct Facts {
        int here;
        int dir;
        bool home;
        Paths mine;
        std::vector<int> enemy;
        int home_distance = 0;
        int home_dir = no_direction;
        int threat = unreachable;
        int enemy_speed;

        explicit Facts(const Board& b)
            : here(b.cell(b.turn.beams[b.init.seat].x, b.turn.beams[b.init.seat].y)),
              dir(static_cast<int>(std::string_view(directions).find(b.turn.beams[b.init.seat].dir))),
              home(b.owner[here] == b.own), mine(my_paths(b)), enemy(enemy_times(b)),
              enemy_speed(b.turn.beams[1 - b.init.seat].boost_left > 0 ? 2 : 1) {
            if (!home) home_distance = unreachable;
            for (int k = 0; k < b.count; ++k) {
                if (!home && b.owner[k] == b.own && mine.distance[k] < home_distance) {
                    home_distance = mine.distance[k];
                    home_dir = mine.first[k];
                }
                if (b.contour[k] == b.own) threat = std::min(threat, enemy[k]);
            }
        }

        Paths my_paths(const Board& b) const {
            const auto blocked = b.trail(b.own);
            return b.paths(here, dir ^ 1, &blocked);
        }

        static std::vector<int> enemy_times(const Board& b) {
            const auto& beam = b.turn.beams[1 - b.init.seat];
            const auto& core = b.init.calib[1 - b.init.seat];
            auto result = b.paths(beam.alive ? b.cell(beam.x, beam.y) : b.cell(core.x, core.y)).distance;
            const int speed = beam.alive && beam.boost_left > 0 ? 2 : 1;
            for (int& value : result) {
                if (value != unreachable) value = beam.restart_in + (value + speed - 1) / speed;
            }
            return result;
        }
    };

    struct Candidate {
        Route moves;
        double value = -unreachable;
        bool risky = true;
    };

    const bot_sdk::InitState init_;
    bool search_;
    bool unlimited_;
    std::string terrain_;
    std::vector<std::array<int, 4>> neighbors_;
    Mask protected_;
    Route plan_;
    Clock::time_point deadline_;

    bool over_budget() const { return !unlimited_ && Clock::now() >= deadline_; }
    static std::string command(int d) { return std::string(1, directions[d]); }
    static bool safe(const Board& b, const Facts& f, int d) {
        const int k = b.neighbors[f.here][d];
        return d != (f.dir ^ 1) && b.passable(k) && b.contour[k] != b.own;
    }

    std::string divert(const Board& b, const Facts& f, int d) {
        plan_.clear();
        return command(dodge(b, f, d));
    }

    int dodge(const Board& b, const Facts& f, int d) const {
        const auto& beam = b.turn.beams[1 - b.init.seat];
        const int target = b.neighbors[f.here][d], enemy = b.cell(beam.x, beam.y);
        if (!search_ || !beam.alive || target < 0 || b.owner[target] == b.own
                || b.distance(enemy, target) > f.enemy_speed) return d;
        for (int alt = 0; alt < 4; ++alt) {
            if (alt == d || !safe(b, f, alt)) continue;
            const int k = b.neighbors[f.here][alt];
            if (b.owner[k] == b.own || b.distance(enemy, k) > f.enemy_speed) return alt;
        }
        return d;
    }

    int attack(const Board& b, const Facts& f) const {
        if (!f.home && f.home_distance + 1 >= f.threat) return no_direction;
        const auto& beam = b.turn.beams[1 - b.init.seat];
        if (search_ && !beam.alive) return no_direction;
        if (search_) {
            const int position = b.cell(beam.x, beam.y);
            const int dir = static_cast<int>(std::string_view(directions).find(beam.dir));
            const auto blocked = b.trail(b.enemy);
            const auto paths = b.paths(position, dir ^ 1, &blocked);
            int back = unreachable;
            int cut = -1;
            for (int k = 0; k < b.count; ++k) {
                const int distance = f.mine.distance[k];
                if (b.owner[k] == b.enemy && paths.distance[k] > 0) back = std::min(back, paths.distance[k]);
                if (b.contour[k] == b.enemy && distance > 0 && distance <= 20
                        && (cut < 0 || distance < f.mine.distance[cut])) cut = k;
            }
            if (cut >= 0 && f.mine.distance[cut] <= (back + f.enemy_speed - 1) / f.enemy_speed) {
                return f.mine.first[cut];
            }
        }
        int best = search_ ? 13 : 9, target = no_direction;
        for (int k = 0; k < b.count; ++k) {
            const int distance = f.mine.distance[k];
            const bool latent = !b.latent.empty() && b.latent[k] != '.' && b.owner[k] == b.enemy;
            if (distance > 0 && distance < best
                    && ((!search_ && b.contour[k] == b.enemy) || (latent && (!search_ || distance <= b.latent[k] - '0')))) {
                best = distance;
                target = f.mine.first[k];
            }
        }
        return target;
    }

    int item(const Board& b, const Facts& f) const {
        if (!f.home && f.home_distance + 1 >= f.threat) return no_direction;
        const int limit = search_ ? 16 : 12;
        if (b.turn.boost) {
            const int k = b.cell(b.turn.boost->x, b.turn.boost->y), d = f.mine.distance[k];
            if (d > 0 && d <= limit && (d < f.enemy[k] || (search_ && d == f.enemy[k] && b.owner[k] == b.own))) {
                return f.mine.first[k];
            }
        }
        if (search_ && b.turn.boost_next) {
            const auto& next = *b.turn.boost_next;
            const int k = b.cell(next.x, next.y), d = f.mine.distance[k];
            if (d > 0 && d <= limit && d <= f.enemy[k] && b.turn.turn - 1 + d >= next.at + 1) {
                return f.mine.first[k];
            }
        }
        return no_direction;
    }

    double value(const Board& b, const std::vector<int>& captured, int length) const {
        const auto& order = b.turn.order;
        const bool final = !b.latent.empty();
        const bool in_time = order && b.turn.turn - 1 + length + (final ? 9 : 1) <= order->until;
        int points = 0;
        bool steals = false;
        for (int k : captured) {
            const int x = k % b.size, y = k / b.size;
            const int weight = b.value(k);
            points += weight;
            if (b.owner[k] == b.enemy) {
                points += weight;
                steals = true;
            }
            if (in_time && x >= order->rect.x0 && x <= order->rect.x1
                    && y >= order->rect.y0 && y <= order->rect.y1) points += weight;
            if (search_ && (b.turn.boost || b.turn.boost_next)) {
                const int bx = b.turn.boost ? b.turn.boost->x : b.turn.boost_next->x;
                const int by = b.turn.boost ? b.turn.boost->y : b.turn.boost_next->y;
                const int distance = std::max(std::abs(x - bx), std::abs(y - by));
                points += distance == 0 ? 10 : distance <= 2 ? 4 : 0;
            }
        }
        if (!search_) return points * 10 - length;
        const double disconnected = final && steals ? 0.6 * b.disconnected_value(captured) : 0.0;
        return (points + disconnected) / (length + 8.0);
    }

    Candidate search_from(const Board& b, const Facts& f, int start, int dir, const Route& prefix) const {
        static const std::vector<std::pair<int, int>> short_sizes = {{4,6}, {5,8}, {6,9}, {5,7}, {3,5}, {3,3}, {2,2}};
        static const std::vector<std::pair<int, int>> long_sizes = {
            {3,4}, {4,6}, {5,8}, {6,9}, {5,6}, {7,10}, {3,6}, {2,4}, {3,3}, {2,2}, {8,12}, {10,14}, {12,18}, {14,20}};
        Candidate best;
        for (int first = 0; first < 4; ++first) {
            if (first == (dir ^ 1)) continue;
            int exit = start;
            Route inside = prefix;
            for (int steps = 0; steps < 30; ++steps) {
                const int next = b.neighbors[exit][first];
                if (!b.passable(next) || b.owner[next] != b.own) break;
                exit = next;
                inside.push_back(first);
            }
            for (int second = (first < 2 ? 2 : 0); second < (first < 2 ? 4 : 2); ++second) {
                for (const auto& [a, c] : search_ ? long_sizes : short_sizes) {
                    if (over_budget()) return best;
                    auto blocked = b.trail(b.own);
                    Mask trail(b.count);
                    Route moves = inside;
                    int k = exit;
                    for (int step = 0; step < a + c; ++step) {
                        const int d = step < a ? first : second;
                        const int next = b.neighbors[k][d];
                        if (!b.passable(next) || blocked[next] || b.owner[next] == b.own) {
                            k = -1;
                            break;
                        }
                        k = next;
                        blocked[k] = trail[k] = 1;
                        moves.push_back(d);
                    }
                    if (k < 0) continue;
                    const auto paths = b.paths(k, second ^ 1, &blocked);
                    int home = -1, back = a + c + (search_ ? 12 : 6) + 1;
                    for (int tile = 0; tile < b.count; ++tile) {
                        if (b.owner[tile] == b.own && paths.distance[tile] < back) {
                            home = tile;
                            back = paths.distance[tile];
                        }
                    }
                    if (home < 0) continue;
                    for (int d : paths.route(home)) {
                        k = b.neighbors[k][d];
                        trail[k] = b.owner[k] != b.own;
                        moves.push_back(d);
                    }
                    const int length = static_cast<int>(moves.size());
                    bool risky = false;
                    for (int tile = 0; tile < b.count; ++tile) {
                        if (trail[tile] && f.enemy[tile] <= length) {
                            risky = true;
                            break;
                        }
                    }
                    if (risky && length > 40) continue;
                    const auto captured = b.captured(trail);
                    if (captured.empty()) continue;
                    const double score = value(b, captured, length);
                    if (score > best.value) best = {std::move(moves), score, risky};
                }
            }
        }
        return best;
    }

    Route make_plan(const Board& b, const Facts& f) const {
        auto best = search_from(b, f, f.here, f.dir, {});
        if (!best.moves.empty() && !best.risky) return best.moves;
        std::vector<std::tuple<int, int, int>> edges;
        for (int k = 0; k < b.count; ++k) {
            const int d = f.mine.distance[k];
            if (b.owner[k] == b.own && d > 0 && d < unreachable && b.edge(k)) {
                edges.emplace_back(d - f.enemy[k], d, k);
            }
        }
        std::sort(edges.begin(), edges.end());
        std::vector<int> used;
        for (const auto& [priority, distance, k] : edges) {
            if (over_budget() || used.size() >= (search_ ? 4u : 3u)) break;
            if (distance > 20 || std::any_of(used.begin(), used.end(), [&](int old) { return b.distance(k, old) < 6; })) continue;
            auto prefix = f.mine.route(k);
            used.push_back(k);
            auto candidate = search_from(b, f, k, prefix.back(), prefix);
            if (!candidate.risky && (best.risky || candidate.value > best.value)) best = std::move(candidate);
        }
        if (!best.moves.empty()) return best.moves;
        if (!edges.empty()) {
            const auto nearest = std::min_element(edges.begin(), edges.end(), [](const auto& a, const auto& b) {
                return std::tie(std::get<1>(a), std::get<2>(a)) < std::tie(std::get<1>(b), std::get<2>(b));
            });
            return f.mine.route(std::get<2>(*nearest));
        }
        return {f.dir};
    }
};

}  // namespace baseline
