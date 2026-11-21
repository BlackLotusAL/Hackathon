#include "arena_strategy.hpp"
#include "strategy.hpp"

#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace {

int read_int(std::istream& input) {
    int number;
    if (!(input >> number)) {
        throw std::runtime_error("missing integer from gateway");
    }
    return number;
}

int read_size(std::istream& input) {
    const int size = read_int(input);
    if (size < 0 || size > 65536) {
        throw std::runtime_error("invalid message size from gateway");
    }
    return size;
}

std::string read_text(std::istream& input) {
    // Strings are framed by their UTF-8 byte count. Map IDs can contain whitespace.
    std::string text(read_size(input), '\0');
    if (input.get() != '\n' || !input.read(text.data(), text.size()) || input.get() != '\n') {
        throw std::runtime_error("invalid string from gateway");
    }
    return text;
}

template <typename Read>
auto read_list(std::istream& input, Read read) {
    const int size = read_size(input);
    std::vector<decltype(read(input))> values;
    values.reserve(size);
    for (int i = 0; i < size; ++i) {
        values.push_back(read(input));
    }
    return values;
}

arena::Point read_point(std::istream& input) {
    return {read_int(input), read_int(input)};
}

arena::Rect read_rect(std::istream& input) {
    return {read_int(input), read_int(input), read_int(input), read_int(input)};
}

arena::Beam read_beam(std::istream& input) {
    const int seat = read_int(input);
    const auto position = read_point(input);
    const auto direction = read_text(input);
    if (direction.size() != 1 || std::string_view("UDLR").find(direction[0]) == std::string_view::npos) {
        throw std::runtime_error("invalid beam direction from gateway");
    }
    return {seat, position.x, position.y, direction[0], read_int(input), read_int(input) != 0,
            read_int(input), read_int(input)};
}

arena::InitState read_init(std::istream& input) {
    return {read_int(input), read_int(input), read_int(input), read_text(input), read_text(input),
            read_list(input, read_text), {read_point(input), read_point(input)},
            read_list(input, read_rect), read_list(input, read_point)};
}

arena::TurnState read_turn(std::istream& input) {
    arena::TurnState state{
        read_int(input), read_list(input, read_text), read_list(input, read_text), read_list(input, read_text),
        {read_beam(input), read_beam(input)}, {read_int(input), read_int(input)}, {read_int(input), read_int(input)},
        {}, {}, {}};
    if (read_int(input)) {
        state.order = arena::Order{read_int(input), read_rect(input), read_int(input)};
    }
    if (read_int(input)) {
        state.boost = read_point(input);
    }
    if (read_int(input)) {
        const auto point = read_point(input);
        state.boost_next = arena::NextBoost{point.x, point.y, read_int(input)};
    }
    return state;
}

bool valid_command(std::string_view cmd) {
    return (cmd.size() == 1 || (cmd.size() == 2 && cmd[1] == '+'))
        && std::string_view("UDLR").find(cmd[0]) != std::string_view::npos;
}

void run(std::istream& input, std::ostream& reply) {
    char message;
    if (!(input >> message) || message != 'I') {
        throw std::runtime_error("expected initialization from gateway");
    }
    const auto init = read_init(input);
    std::optional<Strategy> strategy;
    try {
        strategy.emplace(init);
    } catch (...) {
        std::cerr << "Strategy constructor threw an exception\n";
        reply << "!\n" << std::flush;
        // Let the gateway observe the initialization error and close this worker.
        input.ignore(std::numeric_limits<std::streamsize>::max());
        return;
    }
    reply << "OK\n" << std::flush;
    while (input >> message) {
        if (message != 'T') {
            throw std::runtime_error("expected a turn from gateway");
        }
        const auto state = read_turn(input);
        try {
            const std::string cmd = strategy->act(state);
            reply << (valid_command(cmd) ? cmd : "!") << '\n' << std::flush;
        } catch (...) {
            std::cerr << "Strategy::act threw an exception\n";
            reply << "!\n" << std::flush;
        }
    }
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "expected a gateway reply pipe\n";
        return EXIT_FAILURE;
    }
    std::ofstream reply(argv[1], std::ios::binary);
    if (!reply) {
        std::cerr << "could not open the gateway reply pipe\n";
        return EXIT_FAILURE;
    }
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    try {
        run(std::cin, reply);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
