# team002 · 贪心 · C++

本示例为 C++ 贪心策略。准备 Python 3.9 或以上版本、Docker 和 Buildx。首次构建或缓存缺失时需要基础镜像和 CMake 依赖，可用 `ARENA_CMAKE_INDEX_URL` 设置构建依赖源。

在项目的 `player-kit/` 目录依次自检和试打。工具自动构建并启动镜像，试打完成后保存回放和日志。

```sh
python -m exposure check ../teams/team002
python -m exposure match ../teams/team002 bots/bot002 --map R42
```
