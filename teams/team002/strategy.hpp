#pragma once

#include "planner.hpp"

class Strategy {
public:
    explicit Strategy(const bot_sdk::InitState& init) : planner_(init, false) {}

    std::string act(const bot_sdk::TurnState& state) { return planner_.act(state); }

private:
    baseline::Planner planner_;
};
