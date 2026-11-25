# 曝光领地 · 选手材料包（初赛）

本地对战使用 `engine/`，由它管理 Bot 进程和 HTTP 通信。开发接口和构建适配按语言放在 `sdk/python/`、`sdk/cpp/` 和 `sdk/java/`。

## 初赛回合接口

每回合只返回一个方向字符 `U`、`D`、`L` 或 `R`。开局消息的 `boost_points` 依次给出 A、B、C 的中心。第 1～100 回合点亮 A，随后每 100 回合换站，六轮依次为 A、B、C、A、B、C。回合消息的 `active_station` 给出本回合亮站的中心坐标。从它周围 3×3 可通行格起步时，同一方向自动走两步，每步单独结算。回合开始时，在场束斑的 `speed` 来自上一已结算局面，本回合步数可用下表的接口查询。

| 语言 | 读取本回合的亮站 | 查询起步格的移动步数 |
| --- | --- | --- |
| Python | `state.active_station` | `state.move_steps(state.me)` |
| C++ | `init.active_station(state.turn)` | `init.move_steps(state.turn, x, y)` |
| Java | `init.activeStation(state.turn())` | `init.moveSteps(state.turn(), x, y)` |

两个座位的累计领先回合数和响应时间（整数微秒）随初赛回合消息提供，字段为 `lead_turns` 和 `time_us`。Java 对应 `leadTurns()` 和 `timeUs()`。

`score` 已扣除周长成本。全队校准区以外领地的露边每满 3 条扣 1 分，普通格面值 1、关键格面值 3。双方同时技术判负时，回放结果的 `winner` 为 `null`，`reason` 为 `double_loss`。

## 本地对战

使用 Python 3.9 或以上版本。运行 C++ Bot 还需要 CMake 3.16 或以上版本和 C++17 工具链，运行 Java Bot 还需要 JDK 17 和 Maven。C++ 和 Java 会在临时目录构建，再以本机进程运行。

解压材料包后，在根目录按语言运行对应命令。本地引擎为双方各启动一个进程，在 `127.0.0.1` 的不同端口提供 HTTP 服务。双方请求使用同一个 `run_id`，无需配置令牌。

```sh
python -m engine bots/my_bot_python
python -m engine bots/my_bot_cpp
python -m engine bots/my_bot_java
```

命令会检查提交文件，启动 Bot，并在种子 42 的地图上与贪心基准 `bot002` 打一局。终端显示胜方、得分、已完成回合数和响应错误。正常打满 600 回合时命令返回 0，提前技术判负或启动失败时返回 1。

每局结算后，包括技术判负，命令会将回放 JSON 保存到当前目录的 `replays/`，并在终端显示文件路径。

## 查看回放

直接在浏览器打开材料包根目录的 [replay.html](replay.html)。页面会自动列出 `replays/` 中随包提供的三场基准 Bot 对战，并载入列表第一场。对局结束后，刷新页面即可查看新回放。

从下拉列表切换对局，用播放、逐回合前后切换或进度条查看棋盘、得分、指令和事件。也可以点击 **选择回放文件夹** 或 **打开 JSON** 查看其他回放。

## 开发自己的 Bot

在材料包根目录运行 `git -C bots clone "<本队仓库地址>"`。Git 会按本队的仓库名在 `bots/` 下创建目录，例如 `bots/team001`。

将下表中一个模板目录的内容复制到本队仓库根目录，保留 UTF-8 的 `bot.json` 和 `README.md`，在 README 中说明策略、使用方法、依赖和代码来源。

| 语言 | 模板 | 修改 |
| --- | --- | --- |
| Python | [my_bot_python](bots/my_bot_python/) | 在 `strategy.py` 实现 `Strategy.act` |
| C++ | [my_bot_cpp](bots/my_bot_cpp/) | 在 `strategy.hpp` 实现 `Strategy` |
| Java | [my_bot_java](bots/my_bot_java/) | 在根目录的 `Strategy.java` 实现策略 |

从材料包根目录传入本队仓库的路径。下面以 `bots/team001` 为例。

```sh
python -m engine bots/team001 --opponent bot001 --seed 42
```

`--opponent` 可选 `bot001`（随机）、`bot002`（贪心）或 `bot003`（搜索）。用 `--seed` 更换地图。

内置 Python Bot 只依赖标准库。自己的额外依赖在运行工具的 Python 环境中安装，例如 `python -m pip install -r bots/team001/requirements.txt`。各语言的构建和提交文件说明见对应模板。
