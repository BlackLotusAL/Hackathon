#pragma once

#include "bot_sdk.hpp"

#error "team004: intentional C++ build failure"

class Strategy {
public:
    explicit Strategy(const bot_sdk::InitState&) {}

    std::string act(const bot_sdk::TurnState&) {
        return "U";
    }
};
