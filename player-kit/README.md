# 曝光领地 · 选手材料包（初赛）

本地试打由 `engine/` 运行，包括 Bot 进程管理和 HTTP 通信。开发接口和构建适配按语言放在 `sdk/python/`、`sdk/cpp/` 和 `sdk/java/`。

## 本地试打

使用 Python 3.9 或以上版本。试打 C++ 需本机 CMake 3.16 或以上版本和 C++17 工具链。试打 Java 需本机 JDK 17 和 Maven。C++ 和 Java 会在临时目录构建，再以本机进程运行。

解压材料包后，在根目录按语言运行对应命令。试打为双方各启动一个进程，在 `127.0.0.1` 的不同端口提供 HTTP 服务。双方请求使用同一个 `run_id`，无需配置令牌。

```sh
python -m engine bots/my_bot_python
python -m engine bots/my_bot_cpp
python -m engine bots/my_bot_java
```

命令会检查提交文件，启动 Bot，并在种子 42 的地图上与贪心基准 `bot002` 打一局。终端显示胜方、得分、已完成回合数和响应错误。正常打满 600 回合时命令返回 0，提前技术判负或启动失败时返回 1。

每局结算后，包括技术判负，命令会将回放 JSON 保存到当前目录的 `replays/`，并在终端显示文件路径。

## 查看回放

直接在浏览器打开材料包根目录的 [replay.html](replay.html)。页面会自动列出 `replays/` 中随包提供的三场基准 Bot 对战，并载入列表第一场。试打结束后，刷新页面即可查看新回放。

从下拉列表切换对局，用播放、逐回合前后切换或进度条查看棋盘、得分、指令和事件。也可以点击 **选择回放文件夹** 或 **打开 JSON** 查看其他回放。

## 开发自己的 Bot

将下表中一个模板目录的内容复制到本队仓库根目录，保留 UTF-8 的 `bot.json` 和 `README.md`，在 README 中说明策略、使用方法、依赖和代码来源。

| 语言 | 模板 | 修改 |
| --- | --- | --- |
| Python | [my_bot_python](bots/my_bot_python/) | 在 `strategy.py` 实现 `Strategy.act` |
| C++ | [my_bot_cpp](bots/my_bot_cpp/) | 在 `strategy.hpp` 实现 `Strategy` |
| Java | [my_bot_java](bots/my_bot_java/) | 在根目录的 `Strategy.java` 实现策略 |

从材料包根目录传入实际提交目录。

```sh
python -m engine ../my-bot --opponent bot001 --seed 42
```

`--opponent` 可选 `bot001`（随机）、`bot002`（贪心）或 `bot003`（搜索）。用 `--seed` 更换地图。

内置 Python Bot 只依赖标准库。自己的额外依赖在运行工具的 Python 环境中安装，例如 `python -m pip install -r ../my-bot/requirements.txt`。各语言的构建和提交文件说明见对应模板。
