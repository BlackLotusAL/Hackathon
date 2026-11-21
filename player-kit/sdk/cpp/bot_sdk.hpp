#pragma once

#include <array>
#include <optional>
#include <string>
#include <vector>

namespace bot_sdk {

struct Point {
    int x;
    int y;
};

struct Rect {
    int x0;
    int y0;
    int x1;
    int y1;
};

struct InitState {
    int seat;
    int size;
    int max_turns;
    std::string stage;
    std::string map;
    std::vector<std::string> terrain;
    std::array<Point, 2> calib;
    std::vector<Rect> orders;
    std::vector<Point> boost_points;
};

struct Beam {
    int seat;
    int x;
    int y;
    char dir;
    int speed;
    bool alive;
    int restart_in;
    int boost_left;
};

struct Order {
    int no;
    Rect rect;
    int until;
};

struct NextBoost {
    int x;
    int y;
    int at;
};

struct TurnState {
    int turn;
    std::vector<std::string> owner;
    std::vector<std::string> contour;
    std::vector<std::string> latent;  // Empty when the referee omits the field.
    std::array<Beam, 2> beams;
    std::array<int, 2> score;
    std::array<int, 2> bonus;
    std::optional<Order> order;
    std::optional<Point> boost;
    std::optional<NextBoost> boost_next;
};

}  // namespace bot_sdk
