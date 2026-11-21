# C++ 策略模板

在 `strategy.hpp` 中实现 `Strategy`。新增源码时，在 `CMakeLists.txt` 中用 `target_sources(bot PRIVATE ...)` 登记。

准备 Python 3.9 或以上版本、Docker 和 Buildx，首次构建需要基础镜像和 CMake 依赖。可用 `ARENA_CMAKE_INDEX_URL` 指定依赖源。在包含 `exposure.py` 的材料包根目录运行自检。

```sh
python -m exposure check bots/my_bot_cpp
```

提交时将模板内容放到本队仓库根目录，保留 `bot.json`、`README.md`、`strategy.hpp`、`CMakeLists.txt`。在 README 说明策略、依赖和代码来源，以实际提交目录自检后推送到 `main`。
