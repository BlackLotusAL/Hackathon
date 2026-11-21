# Contest

Contest 通过 HTTP 接收天梯和决赛对决，向 Arena 提交单局，从共享归档复核结果。初赛单循环通过后台导入名单、编排并导出赛果。

## 从零启动本地后台

部署环境为 **Linux、CPython 3.9.11**。服务和测试仅使用 Python 标准库。准备好 `python3.9` 和 `venv` 后，从项目根目录运行。

```sh
cd contest
python3.9 -c 'import sys; assert sys.version_info[:3] == (3, 9, 11), sys.version'
python3.9 -m venv .venv
cp -n config/local.example.json config/local.json
```

按照本地模板填写配置，再启动后台。[配置文件](#配置文件)

```sh
.venv/bin/python -B -m service --config config/local.json serve
```

打开初赛管理后台，本地模板账号为 `organizer`，密码为 `contest-local`。局域网客户端使用服务器实际 IP 和配置的端口访问 `/admin/prelim`。终端按 Ctrl+C 停止服务。[本机初赛管理后台](http://127.0.0.1:8082/admin/prelim)

1. 选择 `队伍名单.xlsx` 并点击 **导入名单**。工作簿包含「队伍」工作表，首行为「队伍编号」「队名」。编号写作 `team001` 这类文本，每队一行且唯一，队名可省略。
2. 点击 **创建单循环**，查看对决进度和待处理记录。
3. 所有对决完成后点击 **确认赛果**，再点击 **下载初赛结果**。导出的 `初赛结果.xlsx` 包含「排名」「对决」「对局」三张工作表，供 Portal 整本导入。

## 配置文件

`config/local.example.json` 是本地配置模板，`config/production.example.json` 是正式配置模板。两者均通过 HTTP 对接 Arena。

配置文件中，`archive_root` 的相对路径以 Contest 安装目录为基准，`db` 和 `maps` 的相对路径以配置文件所在目录为基准。命令行路径相对于当前工作目录。取值优先级为命令行参数、配置文件、内置默认值。

| 字段 | 用途 |
| --- | --- |
| `db` | SQLite 数据库路径，必填。首次启动创建父目录和表 |
| `arena_url` | 启动服务时必填，填写 Arena 的 HTTP 基础地址。可用 `serve --arena-url` 覆盖 |
| `archive_root` | 启动服务时必填，填写与 Arena 共享的归档根目录。可用 `serve --archive-root` 覆盖 |
| `maps` | 封存地图池配置路径，执行正式对决前配置相应赛段的地图池 |
| `host` | 监听 IP，默认 `0.0.0.0`，可配置单个 IPv4 或 IPv6 地址 |
| `port` | 监听端口，默认 `8082` |
| `ip_whitelist` | 后台页面和 `/admin/*` 的 TCP 来源 IP 白名单。`null` 放行所有来源，空数组拒绝所有来源，数组可列出单个 IP 或 CIDR 网段。白名单放行后仍须账号密码认证 |
| `admin_user`、`admin_password` | 主办方后台账号和密码，成对填写。均为 `null` 时关闭后台 |

局域网访问后台时，可把 `ip_whitelist` 设为 `["192.168.10.15", "192.168.20.0/24"]` 等实际来源。服务按配置的地址监听，服务器网络须允许访问对应 TCP 端口。

## Portal 联调

以下请求使用本地模板的 `http://127.0.0.1:8082`。POST 提交 UTF-8 JSON，持久化后返回 `202` 和空响应体。同一编号和内容可重复提交。两个 GET 以原编号取状态和结果，待定时 `state` 为 `in_progress`、`result` 为 `null`。

```sh
curl -i -X POST 'http://127.0.0.1:8082/contest/v1/duels' \
  -H 'Content-Type: application/json' \
  --data-binary '{"duel_id":"duel-local-1","rules":"prelim","sides":["team001","team002"],"map_seed":42}'
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-local-1/status'
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-local-1/result'
```

示例为初赛规则的天梯请求，双方编号和种子可替换。正式对决省略 `map_seed` 或将其设为与 `rules` 一致的 `prelim` 或 `final`，并配置对应的封存地图池。

## 正式部署

在 `contest/` 目录创建正式配置。

```sh
cp -n config/production.example.json config/production.json
```

填写正式后台账号密码、Arena 地址、共享归档根目录和地图池路径。Arena 地址模板为 `http://127.0.0.1:18080`，须指向实际监听端口。Portal 配置可访问的 Contest 地址。三个服务可使用不同的挂载路径，但各自的归档根目录须指向同一存储。

Contest 通过 `POST /arena/v1/runs` 提交单局。正式初赛每 10 秒通过 `POST /arena/v1/runs/query` 查询待处理的当前尝试，每批最多 100 个编号。天梯和决赛使用 `GET /arena/v1/runs/{run_id}`，运行统计使用 `GET /arena/v1/status`。启动新版初赛流程前，Arena 须支持批量查询接口。 [Arena 状态查询契约](<../docs/Arena 与 Contest 对接说明.md#33-批量查询执行>)

Arena 对归档有写权限，Contest、Portal 只读。完成的执行在 `runs/<run_id>/` 下交付 `match.json`、`replay.json` 和 `log.jsonl`，构建失败提供 `match.json`，已有构建日志位于 `builds/<team_id>/<run_id>.log`。

在 `config/map-pools.json` 按 `prelim`、`final` 配置已启用赛段的地图池。每个 `file` 指向单张地图 JSON 文件，路径相对于地图池配置文件，`sha256` 填写对应的公布值。数组顺序为比赛使用顺序。

```json
{
  "prelim": [
    {"file": "../maps/prelim-1.json", "sha256": "<第一张初赛地图的公布哈希>"},
    {"file": "../maps/prelim-2.json", "sha256": "<第二张初赛地图的公布哈希>"},
    {"file": "../maps/prelim-3.json", "sha256": "<第三张初赛地图的公布哈希>"}
  ],
  "final": [
    {"file": "../maps/final-1.json", "sha256": "<第一张决赛地图的公布哈希>"},
    {"file": "../maps/final-2.json", "sha256": "<第二张决赛地图的公布哈希>"},
    {"file": "../maps/final-3.json", "sha256": "<第三张决赛地图的公布哈希>"}
  ]
}
```

准备好正式配置、地图池和共享归档后启动。

```sh
.venv/bin/python -B -m service --config config/production.json serve
```

## 生成地图、复核回放与执行单局

在 `contest/` 目录执行，按实际文件替换种子和回放路径。`mapgen --out` 输出地图数组。

```sh
.venv/bin/python -B -m engine mapgen --seed 42 --stage prelim --out maps.json
.venv/bin/python -B -m engine verify-replay ../player-kit/replays/R104_bot003_vs_bot002.json
```

同时校验封存地图原文件时，在包含 `exposure.py` 的选手材料包目录执行 `python -m exposure verify-replay <回放文件> --map-file <公开地图原文件> --map-sha256 <赛前公布哈希>`。

执行单局前准备两个已启动的 Bot HTTP 服务。下面的 `task.json` 使用初赛规则、种子 42，两个 `bots` 对象按出生点 0、1 排列，`url` 填入各自可访问的 HTTP 服务根地址。

```json
{
  "run_id": "run-1.1",
  "rules": "prelim",
  "config": {"map": {"seed": 42}},
  "bots": [
    {"name": "bot001", "url": "http://127.0.0.1:18101"},
    {"name": "bot002", "url": "http://127.0.0.1:18102"}
  ]
}
```

用材料包里的两个 Python 基准 Bot 联调时，从 `contest/` 在两个终端分别启动它们。

```sh
BOT_HOST=127.0.0.1 BOT_PORT=18101 BOT_TOKEN='' .venv/bin/python -B ../player-kit/backend/runtime/strategy_entry.py ../player-kit/bots/bot001
BOT_HOST=127.0.0.1 BOT_PORT=18102 BOT_TOKEN='' .venv/bin/python -B ../player-kit/backend/runtime/strategy_entry.py ../player-kit/bots/bot002
```

在第三个终端运行裁判，生成 `out/result.json`、`out/replay.json` 和 `out/log.jsonl`。终端按 Ctrl+C 停止两个 Bot。

```sh
.venv/bin/python -B -m judge --task task.json --out out
```

## 构建裁判镜像

在启动了 Docker Engine 的 Linux 服务器上，从项目根目录构建。该镜像以 UID/GID `10001:10001` 运行，默认读取 `/task/task.json`、写入 `/out`。用于 Arena 时，将任务目录只读挂载到 `/task`，将单局输出目录挂载到可写的 `/out`，并让裁判与双方 Bot 处于同一个单局隔离网络。

```sh
docker info
docker build --platform linux/amd64 -f contest/judge/Dockerfile -t exposure-judge:local .
```

## 人工管理

在 `contest/` 目录使用与服务相同的配置文件，并替换对决或执行编号。

| 操作 | 命令 | 适用条件 |
| --- | --- | --- |
| 查看对决 | `.venv/bin/python -B -m service --config config/local.json show duel-1` | 对决已受理 |
| 作废执行 | `.venv/bin/python -B -m service --config config/local.json void run-1.1 "主办方确认的平台故障"` | 执行已提交 |
| 创建下一次执行尝试 | `.venv/bin/python -B -m service --config config/local.json retry run-1.1` | 当前尝试已作废 |
| 重新复核 | `.venv/bin/python -B -m service --config config/local.json recheck run-1.2` | 当前尝试暂缓生效 |
| 按原编号补发 | `.venv/bin/python -B -m service --config config/local.json resend run-1.1` | 当前任务交付未确认或被 Arena 拒绝 |

## 运行测试

在 `contest/` 目录运行 L0 或全部测试。集成测试使用本机 HTTP 夹具和临时归档，覆盖 HTTP 幂等、构建失败与平台故障、归档复核和初赛 XLSX 往返。加 `--list` 查看所选用例。

```sh
.venv/bin/python -B -m tests --level L0
.venv/bin/python -B -m tests --level all
```

也可在项目根目录用裁判镜像运行全部测试。

```sh
docker run --rm --network none \
  --mount "type=bind,source=$(pwd),target=/workspace,readonly" \
  --workdir /workspace/contest \
  --entrypoint python exposure-judge:local \
  -B -m tests --level all
```
