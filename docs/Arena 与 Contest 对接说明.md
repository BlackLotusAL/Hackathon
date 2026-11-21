# Arena 与 Contest 对接说明

本文供 Arena 开发与联调使用，定义任务接收、状态响应、共享归档和裁判容器的接口。

## 1. 接口总览

Arena 与 Contest 部署在同一台 Linux 服务器，接口使用本机 HTTP，接受无认证头的请求。联调示例使用 `http://127.0.0.1:18080`，部署时 Contest 的 `arena_url` 指向 Arena 的实际可访问地址和端口。共享归档根目录见[第 5 章](#5-归档交付要求)。

| 用途 | 方法与路径 | 成功响应 |
| --- | --- | --- |
| 提交单局执行 | `POST /arena/v1/runs` | `202 Accepted`，空响应体 |
| 查询执行状态（支持批量） | `POST /arena/v1/runs/query` | `200 OK`，逐局状态及未找到的编号 |
| 查询整体运行状态 | `GET /arena/v1/status` | `200 OK`，运行统计 JSON |

HTTP 报文与归档中的 JSON、JSONL 使用 UTF-8。错误响应是包含稳定 `code` 的 JSON 对象，可附 `message`。时间采用带 `+08:00` 的 ISO 8601 格式。

调用方的 HTTP 超时为 5 秒。查询接口的成功响应体上限为 64 KiB。

## 2. 提交单局执行

`POST /arena/v1/runs` 接收 `Content-Type: application/json` 的单局任务。持久化受理后即返回 `202 Accepted`，响应体为空。

### 2.1 请求字段

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| `run_id` | string | 必填，由 Contest 分配，匹配 `run-[0-9]+\.[1-9][0-9]*`。格式为 `run-<局号>.<尝试次数>`，局号在同一归档中唯一，尝试次数从 1 开始 |
| `duel_id` | string | 必填，整场对决编号，匹配 `duel-[A-Za-z0-9_-]+`。同场各局和重跑使用同一编号 |
| `rules` | string | 必填，取 `prelim`（初赛）或 `final`（决赛），用于匹配裁判镜像和 Bot 运行环境 |
| `bots` | string[2] | 必填，两个不同的参赛方编号。队伍编号为 `team` 加三位数字，基准编号为 `bot001`、`bot002`、`bot003`。下标 0、1 对应该局的出生点 |
| `config` | object | 必填，原样交付给裁判的对局配置 |
| `config.map` | object | 必填，按下表提供一种地图配置 |

| 地图类型 | `config.map` 字段 |
| --- | --- |
| 天梯生成地图 | `seed` 为 Contest 随机生成并随对决固定的 JSON 整数种子 |
| 固定地图文件 | `file` 为地图文件的完整 UTF-8 文本字符串。`sha256` 为该文本的 SHA-256，以 64 位小写十六进制表示。固定地图池使用赛前已公布的哈希 |

`seed` 和 `file` 互斥。天梯对决交付种子，初赛单循环、正式决赛和独立压测交付固定地图文件。正式决赛使用 `rules: "final"` 和决赛地图池的 `file`、`sha256`。裁判将外层 JSON 解码后的 `file` 编码为 UTF-8 并核验哈希，原有的空白和换行均参与计算。Arena 按收到的 `config` 和 `bots` 顺序执行单局，使用原 `run_id` 查询和归档。

### 2.2 天梯提交示例

本例为 `duel-1901` 第一局的首次尝试，使用初赛规则和种子 42。将请求保存为 `run-7001.1.json`。同场第二局首次使用 `run-7002.1`，沿用种子，`bots` 换为 `["team002", "team001"]`。

```json
{
  "run_id": "run-7001.1",
  "duel_id": "duel-1901",
  "rules": "prelim",
  "bots": ["team001", "team002"],
  "config": {"map": {"seed": 42}}
}
```

```sh
curl -i -X POST 'http://127.0.0.1:18080/arena/v1/runs' \
  -H 'Content-Type: application/json' \
  --data-binary @run-7001.1.json
```

### 2.3 初赛单循环提交示例

本例为初赛单循环 `duel-prelim-789` 第一局的首次尝试。填入该场第一张封存地图的完整文本和已公布的哈希，保存为 `run-7003.1.json`。沿用上一节的提交命令，将 `--data-binary` 的参数改为 `@run-7003.1.json`。

```json
{
  "run_id": "run-7003.1",
  "duel_id": "duel-prelim-789",
  "rules": "prelim",
  "bots": ["team001", "team003"],
  "config": {
    "map": {
      "file": "<封存地图文件的完整文本>",
      "sha256": "<该地图文件已公布的 SHA-256>"
    }
  }
}
```

### 2.4 受理与幂等

| 提交情形 | 响应与约束 |
| --- | --- |
| 新 `run_id` | 持久化请求和受理记录，按受理顺序排队，再返回 `202` |
| 已有 `run_id`，请求内容相同 | 返回 `202`，复用原执行及其当前状态 |
| 已有 `run_id`，请求内容不同 | 返回 `409`，保留原请求和执行记录 |

幂等判断使用完整的 `run_id` 和解码后的 JSON 字段值。对象字段顺序可不同，数组顺序和字符串原值参与比较。同一编号的并发提交至多产生一次执行。受理记录须在返回 `202` 前可按原编号查询，响应丢失或服务重启后也可用原编号补发。

## 3. 执行状态查询

`POST /arena/v1/runs/query` 是所有赛程共用的只读查询接口。请求使用 `Content-Type: application/json`，`run_ids` 包含 1 至 100 个互不重复的完整执行编号，含尝试后缀。查询单局时使用相同接口，例如 `{"run_ids":["run-1201.1"]}`。

### 3.1 批量查询

在同一次请求的 `run_ids` 中放入多局编号。以下示例同时查询三个当前尝试。

```sh
curl -i 'http://127.0.0.1:18080/arena/v1/runs/query' \
  -H 'Content-Type: application/json' \
  --data '{"run_ids":["run-1201.1","run-1207.2","run-1213.1"]}'
```

有效请求返回 `200 OK` 和 `Cache-Control: no-store`。已受理的编号及状态放在 `runs`，未知编号放在 `missing_run_ids`。每个请求编号恰好出现在其中一个数组一次。两个数组的顺序均可与请求不同，Contest 按 `run_id` 匹配。

```json
{
  "runs": [
    {"run_id": "run-1207.2", "state": "finished"},
    {"run_id": "run-1201.1", "state": "running"}
  ],
  "missing_run_ids": ["run-1213.1"]
}
```

如果所有编号均未知，仍返回 `200`，`runs` 为空数组。若请求格式或编号无效、编号重复或数组大小超出 1 至 100 条，返回 `400 INVALID_RUN_QUERY`。HTTP 错误、响应遗漏请求编号或包含重复或未请求编号时，Contest 按整批查询失败处理。

### 3.2 Contest 轮询规则

| 场景 | 查询节奏 |
| --- | --- |
| 正式初赛、独立压测 | 每 10 秒一轮。查询已受理或提交未确认、尚未取得终态的当前尝试。每批最多 100 个编号，按批次顺序发送。例如同一轮有 103 个待查编号时，发出 100 条和 3 条两次请求。上一轮还未结束时跳过当前定时点，查询失败的尝试留到下一轮 |
| 天梯对决、正式决赛 | 每次发送一个编号。返回 `queued` 或 `running` 后至少等待 0.5 秒再查询，失败时从 1 秒退避至最多 30 秒 |

已确认受理的编号进入 `missing_run_ids` 时提示主办方核对。同批其他编号的有效状态仍可落库。

主办方结束压测批次后，Contest 停止提交和查询该批次的单局，忽略在途响应。

### 3.3 单局执行状态

| `state` | 含义 | 返回该状态前须发布的归档 |
| --- | --- | --- |
| `queued` | 已持久化受理，等待槽位 | 无 |
| `running` | 正在构建、执行或发布归档 | 无 |
| `finished` | 裁判正常产出单局结果，包含技术判负和指标全同。Contest 另行复核并确认生效 | `match.json`、`replay.json`、`log.jsonl` |
| `build_failed` | 双方构建结论已确认，至少一方因自身提交构建失败 | [构建失败清单 `match.json`](#52-构建失败清单) |
| `platform_error` | 本次尝试因平台故障终止 | 无 |

`finished`、`build_failed` 和 `platform_error` 是该尝试的终态。`platform_error` 的响应字段固定为 `run_id`、`state` 和 `reason`，其余状态的响应字段固定为 `run_id` 和 `state`。

第 2.2 节中的第一次尝试超时后，`runs` 数组中的对应条目如下。

```json
{"run_id": "run-7001.1", "state": "platform_error", "reason": "wall_timeout"}
```

| `reason` | 含义 |
| --- | --- |
| `judge_crash` | 裁判异常退出、缺少必需输出或输出的执行编号与任务不符 |
| `wall_timeout` | 整局运行超时 |
| `container_start_failed` | 容器因平台故障启动失败 |
| `slot_unhealthy` | 槽位健康检查失败或运行条件不符合要求 |
| `build_platform_error` | 构建阶段因平台故障中断 |

### 3.4 执行恢复与重跑

重启后，已受理的待执行任务继续执行，已保存的终态按原 `run_id` 可查。归档已完整发布、终态尚未保存时，核对归档后恢复终态。已启动且无法恢复的执行置为 `platform_error`，保留已有诊断材料，并按中断阶段填写 `reason`，例如构建中断使用 `build_platform_error`，裁判进程中断使用 `judge_crash`。

平台故障排除后，由主办方从 Contest 发起重跑。例如 `run-7001.1` 超时后，Contest 为同一预定局提交 `run-7001.2`，只递增尝试后缀。为新编号记录独立的状态和归档，原编号保留原终态和证据。重跑复用第 6.1 节的固定版本和天梯开始时间，缺少固定记录时按平台故障处理。

## 4. 查询整体运行状态

`GET /arena/v1/status` 的请求体为空，成功返回 `200 OK` 和运行统计 JSON。所有计数取同一次快照。

```sh
curl -i 'http://127.0.0.1:18080/arena/v1/status'
```

```json
{
  "at": "2026-11-27T14:10:00+08:00",
  "slots": {"idle": 3, "busy": 2, "disabled": 1},
  "queue": 4,
  "done_10m": 12,
  "platform_errors_1h": 1
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `at` | string | 快照时间，带 `+08:00` 的 ISO 8601 |
| `slots` | object | 包含以下三个槽位计数，三项相加为槽位总数 |
| `slots.idle` | integer | 空闲且可调度的槽位数 |
| `slots.busy` | integer | 已分配给构建、启动、对战或回收的槽位数 |
| `slots.disabled` | integer | 暂停调度的槽位数 |
| `queue` | integer | 已受理且仍在排队的执行数 |
| `done_10m` | integer | 最近 10 分钟内状态为 `finished` 的执行次数，包含技术判负 |
| `platform_errors_1h` | integer | 最近 1 小时内状态为 `platform_error` 的执行次数 |

每个槽位同时最多容纳一次执行。两个统计窗口均以 `at` 为截止，按执行结束时间统计，每个 `run_id` 计一次。

## 5. 归档交付要求

Arena 写入共享归档，Contest 和 Portal 以只读方式访问。三端挂载路径可以不同，指向同一份存储。Contest 的 `archive_root` 若为相对路径，以 Contest 安装目录为基准。项目位于 `/srv/hackathon` 时，归档根目录示例为 `/srv/hackathon/contest/var/archives/contest/`。

```text
<archive_root>/
├── runs/
│   └── <run_id>/
│       ├── match.json       # 完成或构建失败的清单
│       ├── replay.json      # 完成时必需
│       ├── log.jsonl        # 完成时必需
│       ├── result.json      # 可保留的裁判原始输出
│       ├── p0.stderr.log    # 出生点 0 的 Bot 输出，若有
│       └── p1.stderr.log    # 出生点 1 的 Bot 输出，若有
└── builds/
    └── <team_id>/
        └── <run_id>.log    # 该队本次构建的日志，若有
```

第 3.3 节列出的必需文件须先在最终路径可读，随后持久化并返回终态。可先在同一文件系统的 `.staging/<run_id>/` 写齐并核对单局文件，再将目录原子重命名为 `runs/<run_id>/`。构建日志按上图路径单独交付。最终归档和已有日志按完整执行编号永久保留。

将实际产生的 Bot 标准输出和标准错误合并到对应的 `p0.stderr.log` 或 `p1.stderr.log`，每份最多 1 MB。构建日志按实际产生情况保留。日志文本使用 UTF-8。资源分配和隔离证据须能按完整 `run_id` 提供。

### 5.1 完成清单

`duel-1901` 第一局重跑完成后，Contest 向 `POST /arena/v1/runs/query` 发送 `{"run_ids":["run-7001.2"]}`，收到以下响应。

```json
{
  "runs": [{"run_id": "run-7001.2", "state": "finished"}],
  "missing_run_ids": []
}
```

随后 Contest 读取 `runs/run-7001.2/match.json`。Arena 从同一次裁判的 `/out/result.json` 取得 `result`，原样保留裁判写入的 `run_id`，发布前核对它与任务的完整 `run_id` 一致。Contest 复核时再次比对该字段与本次执行编号，缺失或不一致时暂缓结算。

```json
{
  "bots": [
    {"team_id": "team001", "commit": "1111111111111111111111111111111111111111"},
    {"team_id": "team002", "commit": "2222222222222222222222222222222222222222"}
  ],
  "duel_started_at": "2026-11-27T14:05:00+08:00",
  "result": {
    "run_id": "run-7001.2",
    "engine_version": "exposure-field/3",
    "map": "R42",
    "map_sha256": "79c625cec70b0666fe29bb8ed8ebb9684c1042344848588c2980c2cfe5f20075",
    "winner": 0,
    "reason": "time",
    "score": [9, 9],
    "bonus": [0, 0],
    "lead_turns": [0, 0],
    "time_us": [600600, 1200600]
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `bots` | 按请求的出生点顺序提供两项，每项包含与请求一致的 `team_id`。参赛队伍必填实际固定的 `commit`，基准 Bot 可省略 `commit`。`bots[].state` 仅用于构建失败清单 |
| `duel_started_at` | 天梯对决必填，使用第 6.1 节保存的时间字符串，格式为带 `+08:00` 的 ISO 8601。初赛单循环、正式决赛和压测可省略 |
| `result.run_id` | 必填字符串，取自裁判原始输出，须与本次完整 `run_id` 一致，含尝试后缀，例如 `run-7001.2` |
| `result.engine_version` | 实际使用的引擎版本 |
| `result.map`、`result.map_sha256` | 实际地图编号及哈希。正式地图的哈希须与请求的 `config.map.sha256` 一致 |
| `result.winner`、`result.reason` | 胜方出生点 `0` 或 `1`（未决胜时为 `null`）及裁判的单局终止原因。按当局的 `bots` 确定参赛方 |
| `result.score`、`result.bonus`、`result.lead_turns`、`result.time_us` | 四项指标均按出生点排列。`time_us` 保留裁判核算的整数微秒，包含触发技术判负而未结算的回合 |
| `result.turns` | 可选，使用裁判输出的已结算回合数 |

### 5.2 构建失败清单

`build_failed` 的 `match.json` 只包含 `bots` 和适用的 `duel_started_at`。双方都须提供已确认的 `bots[].state`，取 `succeeded` 或 `failed`，至少一方因自身提交失败。

下例为初赛单循环中 `duel-prelim-12` 的构建双败清单，路径为 `runs/run-1.1/match.json`。本例省略天梯字段 `duel_started_at`。Contest 用 `run-1.1` 作为结算中的 `build_run_id`。`team001` 的构建日志若存在，位于 `builds/team001/run-1.1.log`。

```json
{
  "bots": [
    {"team_id": "team001", "commit": "1111111111111111111111111111111111111111", "state": "failed"},
    {"team_id": "team002", "commit": "2222222222222222222222222222222222222222", "state": "failed"}
  ]
}
```

构建阶段因平台故障无法确认双方结论时，使用 `platform_error` 和 `build_platform_error`。

### 5.3 回放与裁判观测

`replay.json` 的 `format` 为 `match-replay/2`。

| 字段 | 含义 |
| --- | --- |
| `map`、`stage`、`players`、`params` | 地图、赛段、按出生点排列的双方和规则参数 |
| `init` | 初始网格、束斑和得分 |
| `frames` | 逐回合记录命令、束斑、得分、奖励、订单、道具、事件和格子变化。可选的 `frames[].ms` 供界面显示 |
| `result` | 终局结果及已结算回合数 |
| `engine_version`、`observations` | 所有已完成的单局必填，记录当前引擎版本和按原顺序排列的裁判观测 |

`log.jsonl` 按观测顺序每行保存一个 JSON 对象，双方数组按出生点编号排列。

| 阶段 | 观测字段 |
| --- | --- |
| 开局 | `phase` 为 `startup`。`startup` 记录就绪结果 `ready`、`startup` 或 `crash`，`init` 记录开局请求状态，未发出请求的项为 `null` |
| 回合 | `turn` 从 1 开始，`status` 记录请求状态，`us` 记录按赛题规则计入的整数微秒，未发出请求的项为 `0`。触发技术判负而未结算的回合也记录 |
| 终止 | `phase` 为 `end`，`reason` 为结束原因 |

请求状态取 `ok`、`timeout`、`connection`、`invalid` 或 `crash`。日志条目含 `exits` 时，按出生点记录两个对象或 `null`，对象包含 `exit_code` 和 `oom`。公开回放中对应的 `observations[].exits` 使用两个布尔值。

原样交付裁判生成的 `replay.json` 和 `log.jsonl`。Contest 使用同版引擎复算初始局面、逐回合状态和终局结果，并核对裁判日志。正式计时取 `observations[].us` 的整数微秒。独立复核命令支持用 `--log-file` 核对同一次执行的日志。

已复核的单局需要人工裁定时，Contest 单独保存生效胜方和理由，Arena 继续提供对应 `run_id` 的原始清单、回放与日志。

## 6. 运行裁判

### 6.1 固定参赛版本与开始时间

天梯对决使用对决开始时各队仓库的 `main` 分支提交，基准 Bot 使用当期版本。初赛单循环和正式决赛使用该赛段提交截止、仓库锁定时记录的 `main` 分支最终提交。独立压测使用初赛规则与测试名单中队伍的已固定提交，`duel_id` 以 `duel-load-` 开头，`run_id` 继续由 Contest 在同一编号序列中分配。

首次构建前按 `duel_id` 持久保存参赛方编号与固定版本的对应关系。同场各局及重跑按参赛方编号取固定版本，再按该局 `bots` 的顺序分配到出生点。首次尝试未生成归档时，固定记录仍须保留。

天梯在双方版本确定时保存 `duel_started_at`。同场各局及重跑的清单使用同一时间字符串，保留原有小数精度。

### 6.2 镜像交付与任务文件

Arena 从固定提交的代码树构建 Bot。提交根目录包含 UTF-8 编码的 `bot.json` 和非空的 `README.md`。使用当期选手材料包提供的 SDK 和官方入口，按 `bot.json` 的 `language` 选择构建方式。

| `language` | 提交所需文件 | 构建和启动 |
| --- | --- | --- |
| `python` | `strategy.py`、`requirements.txt` | 在 Bot 运行环境放置与任务 `rules` 对应的 Python 运行包，按 `requirements.txt` 安装提交声明的依赖。以运行包根目录为工作目录，运行 `python -E -s -B -u -m sdk.python <提交目录绝对路径>`，入口加载 `Strategy` |
| `cpp` | `strategy.hpp`、`CMakeLists.txt` | 以材料包的 `sdk/cpp/` 为 CMake 源目录，用 `-DBOT_SUBMISSION=<提交目录绝对路径>` 指定提交并构建 `bot` 目标。可用 `-DBOT_OUTPUT_DIR=<产物目录>` 指定可执行文件的输出目录。Bot 通过 `sdk.cpp.cpp_gateway` 提供 HTTP 接口 |
| `java` | `pom.xml`、`Strategy.java` | 将提交根目录的 `.java` 文件和启动适配器纳入 Maven 的 `bot` 包源码目录，SDK 状态类型放在 `bot.sdk` 包源码目录。镜像使用 `bot.Main` 入口 |

Arena 按任务的 `rules` 提供 Python Bot 运行环境。决赛天梯和正式决赛均使用 `final`。两个赛段提供相同的 Python 策略入口和 SDK 状态类型，选手及基准 Bot 可以沿用同一份提交代码。`engine.rules` 的参数和 `TurnState.game` 的推演须与本局裁判使用的规则一致，决赛推演须支持潜像规则。

C++ 和 Java 的正式 Bot 镜像由 Arena 构建，运行平台为 Linux amd64。Bot 容器内设置 `BOT_HOST=0.0.0.0` 和 `BOT_PORT=8080`，供本局网络内的裁判调用。

裁判镜像包含 `judge` 和 `engine`，运行平台为 Linux amd64，基础镜像使用 Python 3.9.11，当前引擎版本为 `exposure-field/3`。通过镜像仓库或离线包交付时注明适用赛段、标签、digest 和引擎版本。Arena 记录实际使用的镜像及 digest。在含 `contest/` 的源码根目录可构建联调用镜像。

```sh
docker build --platform linux/amd64 -f contest/judge/Dockerfile -t exposure-judge:local .
```

将任务目录只读挂载到 `/task`，入口读取 `/task/task.json`。沿用请求的 `run_id`、`rules` 和 `config`，将两个 `bots` 编号按原顺序换成含各自 HTTP 服务根地址 `url` 的对象。可在 `name` 填写原参赛方编号，裁判将其作为回放显示名。裁判默认使用任务文件的 `rules`，本地执行时可用 `--stage` 覆盖，省略 `rules` 时默认为初赛。

```json
{
  "run_id": "run-7001.2",
  "rules": "prelim",
  "config": {"map": {"seed": 42}},
  "bots": [
    {"name": "team001", "url": "http://bot0:8080"},
    {"name": "team002", "url": "http://bot1:8080"}
  ]
}
```

### 6.3 容器运行与输出

Arena 为本局准备隔离网络，并为双方分别启动一个 Bot 容器。每个容器运行该方的 HTTP 服务。部署时配置当期每方的 CPU 和内存规格，以及整局超时。同局双方使用相同规格、互不共享的计算资源，资源规格在同一赛段内保持一致。

Bot HTTP 接口通过路径前缀 `/v1` 标明协议版本。裁判分别向两个 Bot 地址发送请求，路径均使用任务的完整 `run_id`，含尝试后缀。每个 Bot 服务按 `run_id` 保存对局状态。裁判在 `/out/result.json` 中原样写入任务的 `run_id`，供 Arena 和 Contest 核对归档归属。

| 方法与路径 | 时限与处理 |
| --- | --- |
| `GET /v1/health` | 启动后 10 秒内返回 `200`，且 JSON 的 `ready` 为 `true`。未就绪触发技术判负 |
| `POST /v1/matches/{run_id}/init` | 2 秒内返回 2xx，超时或失败记 1 次错误 |
| `POST /v1/matches/{run_id}/turns/{turn}` | 200 毫秒内返回 `200` 和当前回合的 `turn`、`cmd`。超时、格式无效或连接失败记 1 次错误 |
| `POST /v1/matches/{run_id}/end` | 时限 1 秒，超时或失败维持已确定的结果 |

决赛回合消息包含 `latent` 倒计时网格和 `latent_regions`。`latent_regions` 将潜像区域编号映射到其格子下标数组，下标为 `y * size + x`。Python SDK 同时使用两个字段恢复潜像，以便模拟显影和整块作废。

本局累计 10 次错误或 Bot 自身退出均触发技术判负，因自身内存超限被终止按退出处理。由平台导致的容器启动失败或运行条件故障按第 3.3 节报告 `platform_error`。

镜像入口为 `python -B -u -m judge`。下例使用本地构建的镜像，运行前须创建本局隔离网络、同网络的 Bot 服务及对应目录。正式环境使用所交付的镜像。

```sh
docker run --rm --network arena-run-7001-2 \
  --mount type=bind,source=/srv/arena/tasks/run-7001.2,target=/task,readonly \
  --mount type=bind,source=/srv/hackathon/contest/var/archives/contest/.staging/run-7001.2,target=/out \
  exposure-judge:local
```

`bot0`、`bot1` 在本局网络内可解析。裁判以 UID/GID `10001:10001` 读取 `/task`、写入 `/out`，Arena 为挂载目录配置对应权限。

实际观测到 Bot 容器退出时，由 Arena 在任务目录原子发布对应的 `exits/p0.json` 或 `exits/p1.json`，裁判通过只读挂载查看。`exit_code` 是实际退出码（整数），`oom` 是容器运行时报告的内存超限状态（布尔值）。

```json
{"exit_code": 137, "oom": true}
```

裁判退出码为 `0`、`/out/result.json`、`/out/replay.json`、`/out/log.jsonl` 齐全且通过第 5.1 节的执行编号核对后，按第 5 章生成并发布 `finished` 归档。整局运行超时将执行记为 `platform_error`，原因填 `wall_timeout`。其余裁判异常退出或缺少有效输出的情况使用 `judge_crash`。
