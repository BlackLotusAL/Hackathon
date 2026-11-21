#pragma once

#include "planner.hpp"

class Strategy {
public:
    explicit Strategy(const arena::InitState& init) : planner_(init, false) {}

    std::string act(const arena::TurnState& state) { return planner_.act(state); }

private:
    baseline::Planner planner_;
};
