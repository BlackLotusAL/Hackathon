FROM python:3.9.11-slim-bullseye@sha256:10f6e16011b32cca4e2c7aef249340b401a24b956b89bed761dcd6c99f997a08 AS cmake
ARG ARENA_CMAKE_INDEX_URL=https://pypi.org/simple/
RUN printf '%s\n' 'cmake==3.31.6 --hash=sha256:1c8b05df0602365da91ee6a3336fe57525b137706c4ab5675498f662ae1dbcec' > /tmp/cmake.txt \
    && python -m pip --isolated install --disable-pip-version-check --no-cache-dir \
       --only-binary=:all: --no-deps --require-hashes --index-url "$ARENA_CMAKE_INDEX_URL" \
       --target /tools -r /tmp/cmake.txt

FROM gcc:9.3.0@sha256:3afdc4312da6e714a2127f1a39526943a6e82c6ff6ebe68caa02a75cf35c5fbf AS build
COPY --from=cmake /tools/cmake/data /opt/cmake
WORKDIR /src
COPY . .
COPY --from=runner /CMakeLists.txt /main.cpp /arena_strategy.hpp /opt/runner/cpp/
RUN --network=none test "$(g++ -dumpfullversion)" = 9.3.0 \
    && /opt/cmake/bin/cmake -S /opt/runner/cpp -B /build -DCMAKE_BUILD_TYPE=Release \
       -DCMAKE_CXX_COMPILER=g++ -DARENA_SUBMISSION=/src -DARENA_OUTPUT_DIR=/out \
    && /opt/cmake/bin/cmake --build /build --target bot --parallel 2

FROM python:3.9.11-slim-bullseye@sha256:10f6e16011b32cca4e2c7aef249340b401a24b956b89bed761dcd6c99f997a08
ENV BOT_HOST=0.0.0.0 BOT_PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /bot
COPY . .
COPY --from=build /out/bot /usr/local/bin/exposure-bot
COPY --from=runner /gateway.py /opt/runner/gateway.py
COPY --from=game_core /__init__.py /rules.py /protocol.py /opt/runner/game_core/
COPY --from=bot_backend /__init__.py /opt/runner/backend/
COPY --from=bot_backend /runtime/__init__.py /runtime/sdk.py /runtime/state.py /opt/runner/backend/runtime/
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-E", "-B", "-u", "/opt/runner/gateway.py", "/usr/local/bin/exposure-bot"]
