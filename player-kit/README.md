# 曝光领地 · 选手材料包（初赛）

## 快速开始

整份 `player-kit/` 可以单独解压或复制到任意位置。在包含 `exposure.py` 的目录使用 Python 3.9 或以上版本运行下文命令及工作区 `teams/` 中的示例命令。包内的 Python 模板和基准 Bot 只使用标准库。

```sh
python -m exposure check bots/my_bot_python
python -m exposure match bots/my_bot_python bots/bot002 --map R42 --data-dir ./practice-data
python -m exposure serve --open --data-dir ./practice-data
```

`check` 和 `match` 自动启动 Bot，通过本机回环地址通信。`serve` 启动本地练习网页，默认地址为 `http://127.0.0.1:8080`。`match` 和 `serve` 使用同一个数据目录时，试打回放会出现在网页的对局列表。端口被占用时，可为 `serve` 加上 `--port 8081`。

网页支持暂停、单步、手动操作、批量对战和导入回放。试打时可选 `bots/bot001`（随机）、`bots/bot002`（贪心）或 `bots/bot003`（搜索）作为对手。设置 `ARENA_SEED` 可改变 `bot001` 的策略随机种子。本地复现时可用 `ARENA_NO_BUDGET=1` 关闭 `bot002` 的搜索预算。

## 提交

1. 选择语言模板，将目录内容复制到本队 Codehub 仓库根目录。
2. 按模板修改策略，保留 UTF-8 编码的 `bot.json` 和 `README.md`。README 需说明 Bot 的用途、使用方法和依赖。代码由本队编写，可使用公开开源库和主办方开源基准 Bot，使用时注明来源。
3. 在材料包根目录运行 `python -m exposure check ../team001`，将 `../team001` 换成实际提交目录。通过后将代码推送到本队仓库的 `main` 分支。天梯使用挑战开始时的提交，正式比赛使用对应赛段提交截止、仓库锁定时的最终提交。截止时间以当届赛程公告为准。

| `bot.json` 的 `language` | 模板与语言要求 |
| --- | --- |
| `python` | `bots/my_bot_python/`。在 `strategy.py` 实现 `Strategy.act`，用 `requirements.txt` 声明额外依赖，无依赖时保留空文件 |
| `cpp` | `bots/my_bot_cpp/`。在 `strategy.hpp` 实现 `Strategy`，在 `CMakeLists.txt` 用 `target_sources(bot PRIVATE ...)` 登记新增源文件 |
| `java` | `bots/my_bot_java/`。在 `src/main/java/arena/bot/Strategy.java` 实现策略，在 `pom.xml` 声明新增依赖 |

## 本地工具

练习对手可以填 Bot 目录、HTTP(S) 地址或启动命令。使用包内 Python Bot 时，地图生成和试打可在离线环境中完成，网页与 Bot 使用本机回环地址。自有 Python 策略有额外依赖时，先用运行工具的同一个 Python 环境安装，例如 `python -m pip install -r ../team001/requirements.txt`。

C++ 和 Java 的本地构建使用 Docker 与 Buildx。首次构建或缓存缺失时，需要访问基础镜像仓库和相应的依赖源。可用 `ARENA_CMAKE_INDEX_URL` 或 `ARENA_MAVEN_MIRROR_URL` 指定构建镜像使用的依赖源。

| 命令 | 用途 |
| --- | --- |
| `python -m exposure check <Bot 目录>` | 检查提交并试打 60 回合 |
| `python -m exposure match <Bot A> <Bot B> --map R42` | 用种子 42 对打一局，输出回放和日志 |
| `python -m exposure tournament bots/my_bot_python bots/bot002 --maps 3` | 在三张随机地图上各打两局，每张图的第 2 局互换出生点 |
| `python -m exposure mapgen --seed 42 --out maps.json` | 生成练习地图。将 `ARENA_MAPS` 设为该 JSON 文件的路径可载入自选地图 |
| `python -m exposure build bots/my_bot_cpp -t exposure/cpp:local` | 单独构建并保留 C++ 或 Java 策略镜像 |

在命令末尾加 `--help` 查看参数。快速试打 Python Bot 时，可在对战方的目录前加 `inproc:`，例如 `inproc:bots/bot002`。断点调试可为 `match` 加 `--no-timing`，也可在网页里取消执行时限。

### 本地数据

数据默认保存在用户目录下的 `.exposure-field/practice/`。可通过 `ARENA_DATA_DIR` 设置本地路径，或为 `match`、`tournament`、`serve` 传入优先级更高的 `--data-dir`。

单局产生 `result.json`、`replay.json` 和 `log.jsonl`。循环赛写入 `tournament_*.json`，网页导入的回放保存在 `imports/`。用 `match --out` 指定单局输出目录后，可在网页中导入该目录里的 `replay.json`。

## 复核回放

在材料包根目录运行以下命令。复核正式对局时，把示例路径换成已下载的 `replay.json`，使用与回放对应的版本。复核在本机读取回放文件并重算赛果。

```sh
python -m exposure verify-replay replays/R104_bot003_vs_bot002.json
```

如需核对封存地图，同时传入 `--map-file`（公开的地图原文件）和 `--map-sha256`（赛前公布的哈希）。

## 源码目录

| 目录 | 职责 |
| --- | --- |
| `game_core/` | 游戏规则、地图、Bot 消息协议和回放复核 |
| `backend/` | Bot SDK、运行与构建、对局和循环赛、回放存储及练习服务 |
| `frontend/` | 浏览器里的练习界面 |

`python -m exposure` 通过 `exposure.py` 解析参数和输出结果。命令行与网页共用后端的对局和循环赛逻辑。
