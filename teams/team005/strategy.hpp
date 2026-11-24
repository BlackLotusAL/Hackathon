#pragma once

#include "bot_sdk.hpp"

#include <string>
#include <string_view>

class Strategy {
public:
    explicit Strategy(const bot_sdk::InitState& init) : init_(init) {}

    std::string act(const bot_sdk::TurnState& state) {
        const auto& me = state.beams[init_.seat];
        if (!me.alive) {
            return std::string(1, me.dir);
        }

        static constexpr std::string_view directions = "URDL";
        static constexpr int dx[] = {0, 1, 0, -1};
        static constexpr int dy[] = {-1, 0, 1, 0};
        static constexpr int offsets[] = {0, 1, 3};  // Straight, right, left.
        const auto forward = directions.find(me.dir);
        const char own = static_cast<char>('0' + init_.seat);

        for (int offset : offsets) {
            const auto direction = (forward + offset) % directions.size();
            const int x = me.x + dx[direction];
            const int y = me.y + dy[direction];
            if (x >= 0 && x < init_.size && y >= 0 && y < init_.size &&
                init_.terrain[y][x] != '#' && state.contour[y][x] != own) {
                return std::string(1, directions[direction]);
            }
        }
        return std::string(1, me.dir);
    }

private:
    bot_sdk::InitState init_;
};
