# 选手材料包

## 初赛回合接口

### 开局地图

每局开始时，SDK 向策略传入 `InitState`，其中 `seat` 是本方座位号（0 或 1），`max_turns` 是回合上限，`map` 是整局共用的地图。

地图的 `id`、`stage` 和 `size` 分别是地图编号、赛段和基板边长。`terrain` 用 `.`、`#`、`*` 表示普通格、缺陷格和关键图形格，`calib` 按座位号记录双方出生点。`boost_zones` 中每个加速区包含标记点 `marker` 和覆盖格列表 `cells`。

坐标为 `[x, y]`，左上角为 `[0, 0]`。读取网格时，先取第 `y` 行，再取第 `x` 个字符。

### 回合状态

每次调用 `Strategy.act` 时，`TurnState` 提供本回合行动前的局面。双方数据均按 0 号方、1 号方排列。

| 字段 | 含义 |
| --- | --- |
| `turn` | 本次要执行的回合号，从 1 开始 |
| `owner`、`contour` | 归属与轮廓网格。`.` 表示无主或无轮廓，`0`、`1` 表示所属方 |
| `beams` | 双方束斑的坐标、朝向和在场状态。停机时，`restart_in` 为距重启的回合数 |
| `score` | 双方当前得分，已扣除周长成本 |
| `lead_turns`、`time_us` | 双方累计领先回合数和累计响应时间，耗时以整数微秒计。Java 对应 `leadTurns()`、`timeUs()` |

### 移动与回复

所有加速区整局可用。从任一加速区的覆盖格起步，束斑沿同一方向连续尝试走 2 格，每步单独结算。从区外起步只尝试走 1 格。`beams[].speed` 取上一回合结算后的值，开局值为 1。用束斑当前坐标查询本回合的步数。

| 语言 | 查询起步格 `(x, y)` 的移动步数 |
| --- | --- |
| Python | `state.map.move_steps(x, y)` |
| C++ | `init.map.move_steps(x, y)` |
| Java | `init.map().moveSteps(x, y)` |

在 `Strategy.act` 中返回一个方向字符。`U` 向上，`D` 向下，`L` 向左，`R` 向右。每回合须在 200 毫秒内回复，束斑停机时也要回复。收到有效的反向命令或未收到有效回复时，裁判沿用束斑的当前方向。

自行实现 HTTP 服务时，回合回复使用 JSON，例如 `{"turn": 1, "cmd": "R"}`。回复的 `turn` 须与请求中的回合号一致。

## 本地对战

使用 Python 3.9 或以上版本。运行 C++ Bot 还需要 CMake 3.16 或以上版本和 C++17 工具链，运行 Java Bot 还需要 JDK 17 和 Maven。C++ 和 Java 会在临时目录构建，再以本机进程运行。

解压材料包后，在根目录按语言运行对应命令。本地引擎为双方各启动一个进程，在 `127.0.0.1` 的不同端口提供 HTTP 服务。双方请求使用同一个 `run_id`，无需配置令牌。

```sh
python -m engine my_bot_python bot002
python -m engine my_bot_cpp bot002
python -m engine my_bot_java bot002
```

命令会检查提交文件，启动 Bot，并在随机地图上与贪心基准 `bot002` 打一局。终端显示胜方、得分、已完成回合数和响应错误。正常打满 600 回合时命令返回 0，提前技术判负或启动失败时返回 1。

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

在材料包根目录运行 `python -m engine P1 P2 [--seed SEED]`。`P1` 和 `P2` 均为必填参数，都可填写 `bots/` 下的 Bot 目录名或包含 `bot.json` 的目录路径。

```sh
python -m engine team001 my_bot_python --seed 42
```

省略 `--seed` 时随机选取地图种子。提供整数种子可复用同一张地图。

内置 Python Bot 只依赖标准库。自己的额外依赖在运行工具的 Python 环境中安装，例如 `python -m pip install -r bots/team001/requirements.txt`。各语言的构建和提交文件说明见对应模板。
