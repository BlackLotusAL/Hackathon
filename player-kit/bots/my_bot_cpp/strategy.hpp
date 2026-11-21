#pragma once

#include "bot_sdk.hpp"

#include <string>

class Strategy {
public:
    explicit Strategy(const bot_sdk::InitState& init) : init_(init) {}

    std::string act(const bot_sdk::TurnState& state) {
        const auto& me = state.beams[init_.seat];
        const std::string directions = "UDLR";
        const auto forward = directions.find(me.dir);
        constexpr int dx[] = {0, 0, -1, 1};
        constexpr int dy[] = {-1, 1, 0, 0};

        if (me.alive) {
            for (std::size_t offset = 0; offset < directions.size(); ++offset) {
                const auto d = (forward + offset) % directions.size();
                if (d == (forward ^ 1)) {
                    continue;
                }
                const int x = me.x + dx[d];
                const int y = me.y + dy[d];
                if (x >= 0 && x < init_.size && y >= 0 && y < init_.size &&
                    init_.terrain[y][x] != '#' && state.contour[y][x] != '0' + init_.seat) {
                    return std::string(1, directions[d]);
                }
            }
        }
        return std::string(1, me.dir);
    }

private:
    bot_sdk::InitState init_;
};
