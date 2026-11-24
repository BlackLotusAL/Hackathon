# team007 · 编译失败 · C++

基于仓库的 C++ SDK。`strategy.hpp` 中的 `#error` 会让编译器报出 `team007: intentional C++ build failure`。

使用 Python 3.9 或以上版本、CMake 3.16 或以上版本和 C++17 工具链。在 `player-kit/` 目录运行下面的命令，预期构建中止，退出码为 `1`。

```sh
python -m engine ../teams/team007
```
