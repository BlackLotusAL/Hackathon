#include "bot_sdk.hpp"
#include "strategy.hpp"

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string_view>

#ifdef _WIN32
#include <cstdint>
#include <fcntl.h>
#include <io.h>
#endif

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

bot_sdk::Point read_point(std::istream& input) {
    return {read_int(input), read_int(input)};
}

bot_sdk::Beam read_beam(std::istream& input) {
    const int seat = read_int(input);
    const auto position = read_point(input);
    const auto direction = read_text(input);
    if (direction.size() != 1 || std::string_view("UDLR").find(direction[0]) == std::string_view::npos) {
        throw std::runtime_error("invalid beam direction from gateway");
    }
    return {seat, position.x, position.y, direction[0], read_int(input), read_int(input) != 0,
            read_int(input)};
}

bot_sdk::InitState read_init(std::istream& input) {
    return {read_int(input), read_int(input), read_int(input), read_text(input), read_text(input),
            read_list(input, read_text), {read_point(input), read_point(input)},
            read_list(input, read_point)};
}

bot_sdk::TurnState read_turn(std::istream& input) {
    return {
        read_int(input), read_list(input, read_text), read_list(input, read_text),
        {read_beam(input), read_beam(input)}, {read_int(input), read_int(input)},
        {read_int(input), read_int(input)}, {read_int(input), read_int(input)}};
}

bool valid_command(std::string_view cmd) {
    return cmd.size() == 1 && std::string_view("UDLR").find(cmd[0]) != std::string_view::npos;
}

void send_reply(std::FILE* pipe, std::string_view message) {
    if (std::fwrite(message.data(), 1, message.size(), pipe) != message.size()
            || std::fputc('\n', pipe) == EOF || std::fflush(pipe) == EOF) {
        throw std::runtime_error("could not write to the gateway reply pipe");
    }
}

std::FILE* open_reply_pipe(const char* argument) {
#ifdef _WIN32
    const auto handle = static_cast<std::intptr_t>(std::stoull(argument));
    const int fd = _open_osfhandle(handle, _O_WRONLY | _O_BINARY);
    return fd == -1 ? nullptr : _fdopen(fd, "wb");
#else
    return fdopen(std::stoi(argument), "wb");
#endif
}

void run(std::istream& input, std::FILE* reply) {
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
        send_reply(reply, "!");
        // Let the gateway observe the initialization error and close this worker.
        input.ignore(std::numeric_limits<std::streamsize>::max());
        return;
    }
    send_reply(reply, "OK");
    while (input >> message) {
        if (message != 'T') {
            throw std::runtime_error("expected a turn from gateway");
        }
        const auto state = read_turn(input);
        std::string cmd;
        try {
            cmd = strategy->act(state);
        } catch (...) {
            std::cerr << "Strategy::act threw an exception\n";
        }
        send_reply(reply, valid_command(cmd) ? std::string_view(cmd) : "!");
    }
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "expected a gateway reply pipe\n";
        return EXIT_FAILURE;
    }
    try {
#ifdef _WIN32
        if (_setmode(_fileno(stdin), _O_BINARY) == -1) {
            throw std::runtime_error("could not set the gateway input to binary mode");
        }
#endif
        std::unique_ptr<std::FILE, decltype(&std::fclose)> reply(open_reply_pipe(argv[1]), std::fclose);
        if (!reply) {
            throw std::runtime_error("could not open the gateway reply pipe");
        }
        std::ios::sync_with_stdio(false);
        std::cin.tie(nullptr);
        run(std::cin, reply.get());
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
