cmake_minimum_required(VERSION 3.16)
project(ExposureBot LANGUAGES CXX)

set(ARENA_OUTPUT_DIR "${CMAKE_BINARY_DIR}/bin" CACHE PATH "Bot executable output directory")
add_executable(bot main.cpp)
set_target_properties(bot PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    RUNTIME_OUTPUT_DIRECTORY "$<1:${ARENA_OUTPUT_DIR}>")
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    target_link_options(bot PRIVATE -static-libstdc++ -static-libgcc)
endif()
target_include_directories(bot PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}" "${ARENA_SUBMISSION}")
add_subdirectory("${ARENA_SUBMISSION}" submission)
