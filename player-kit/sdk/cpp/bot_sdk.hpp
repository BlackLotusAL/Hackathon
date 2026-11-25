#pragma once

#include <array>
#include <cstddef>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace bot_sdk {

struct Point {
    int x;
    int y;
};

struct BoostZone {
    Point marker;
    std::vector<Point> cells;
};

class GameMap {
public:
    GameMap(int size, std::string stage, std::string id, std::vector<std::string> terrain,
            std::array<Point, 2> calib, std::vector<BoostZone> zones)
        : size_(size), stage_(std::move(stage)), id_(std::move(id)), terrain_(std::move(terrain)),
          calib_(calib), boost_zones_(std::move(zones)) {
        if (size_ != 41) {
            throw std::invalid_argument("invalid board size");
        }
        boost_cells_.resize(size_ * size_);
        for (const auto& zone : boost_zones_) {
            for (const auto& cell : zone.cells) {
                if (!in_bounds(cell.x, cell.y)) {
                    throw std::invalid_argument("invalid boost cell");
                }
                boost_cells_[cell.y * size_ + cell.x] = true;
            }
        }
    }

    int size() const { return size_; }
    const std::string& id() const { return id_; }
    const std::string& stage() const { return stage_; }
    const std::vector<std::string>& terrain() const { return terrain_; }
    const std::array<Point, 2>& calib() const { return calib_; }
    const std::vector<BoostZone>& boost_zones() const { return boost_zones_; }

    int move_steps(int x, int y) const {
        return in_bounds(x, y) && boost_cells_[y * size_ + x] ? 2 : 1;
    }

private:
    bool in_bounds(int x, int y) const {
        return x >= 0 && y >= 0 && x < size_ && y < size_;
    }

    int size_;
    std::string stage_;
    std::string id_;
    std::vector<std::string> terrain_;
    std::array<Point, 2> calib_;
    std::vector<BoostZone> boost_zones_;
    std::vector<bool> boost_cells_;
};

struct InitState {
    int seat;
    int max_turns;
    GameMap map;
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
