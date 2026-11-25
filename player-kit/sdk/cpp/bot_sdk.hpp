#pragma once

#include <array>
#include <cstdlib>
#include <optional>
#include <string>
#include <vector>

namespace bot_sdk {

struct Point {
    int x;
    int y;
};

struct InitState {
    int seat;
    int size;
    int max_turns;
    std::string stage;
    std::string map;
    std::vector<std::string> terrain;
    std::array<Point, 2> calib;
    std::vector<Point> boost_points;

    std::optional<Point> active_station(int turn) const {
        if (turn < 1 || boost_points.empty()) return std::nullopt;
        return boost_points[((turn - 1) / 100) % boost_points.size()];
    }

    int move_steps(int turn, int x, int y) const {
        auto station = active_station(turn);
        return station && std::abs(x - station->x) <= 1 && std::abs(y - station->y) <= 1 ? 2 : 1;
    }
};

struct Beam {
    int seat;
    int x;
    int y;
    char dir;
    int speed;
    bool alive;
    int restart_in;
};

struct TurnState {
    int turn;
    std::vector<std::string> owner;
    std::vector<std::string> contour;
    std::array<Beam, 2> beams;
    std::array<int, 2> score;
    std::array<int, 2> lead_turns{};
    std::array<int, 2> time_us{};
};

}  // namespace bot_sdk
