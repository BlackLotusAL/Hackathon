# Contest

## 启动

在 Linux 上使用 CPython 3.9.11，从仓库根目录执行。

```sh
cd contest
python3.9 -m venv .venv
cp -n config/contest.example.json config/contest.json
```

编辑 `config/contest.json`，填写 Arena 的 HTTP 地址 `arena_url` 和共享归档目录 `archive_root`，并选择 `phase`。正式部署时替换示例后台账号密码，将 `maps` 指向封存地图池。

```sh
.venv/bin/python -B -m service --config config/contest.json serve
```

打开[管理后台](http://127.0.0.1:8082/admin/prelim)。示例账号为 `organizer`，密码为 `contest-local`。

## 配置文件

| `phase` | 用途 | 地图 |
| --- | --- | --- |
| `prelim-ladder` | 初赛天梯，默认值 | 同一张随机地图，两局换边 |
| `prelim` | 初赛单循环和压测 | `prelim` 地图池，三图六局 |
| `final-ladder` | 决赛天梯 | 同一张随机地图，两局换边 |
| `final` | 正式决赛 | `final` 地图池，三图六局 |

修改 `phase` 后用同一个 `db` 重启服务。收到 `409 PHASE_BUSY` 时，等上一赛程的对决结算后再提交。

- `db` 和 `maps` 的相对路径以服务配置文件所在目录为基准，`archive_root` 以 `contest/` 为基准。
- 正式地图池按比赛顺序配置三张封存地图。 [示例地图池](config/map-pools.example.json) · [配置封存地图](#配置封存地图)
- `host`、`port` 设置监听地址和端口。`ip_whitelist` 填允许访问后台的 IP 或 CIDR 列表。 [配置模板](config/contest.example.json)

## 初赛与压测

将 `phase` 设为 `prelim`，在管理后台操作。

1. 上传 XLSX 并点击 **导入名单**。使用「队伍」工作表，首行为「队伍编号」「队名」，每队一行。 [名单格式](<../docs/Portal 与 Contest 对接说明.md#52-队伍名单>)
2. 点击 **创建单循环**。在 **待处理记录** 中查看故障，修复后重跑。在 **人工裁定** 中填写待裁定场次的胜方和理由。
3. 全部场次结算后点击 **确认赛果** 和 **下载初赛结果**，将 XLSX 导入 Portal。

在 **独立压测** 填批次编号（如 `load-trial-1`），上传至少两队的同格式名单，点 **启动压测**。在 **查看批次** 里看进度或点 **结束压测**。提前结束时，等 Arena 队列和占用槽位清空再启动正式初赛。

## Portal 联调

```sh
curl -i 'http://127.0.0.1:8082/contest/v1/duels' \
  -H 'Content-Type: application/json' \
  --data-binary '{"sides":["team001","team002"]}'

# 将 duel-1901 替换为返回的 duel_id
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/status'
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/result'
```

决赛由 Portal 预排对阵，通过同一接口逐场提交。 [Portal 对接说明](<../docs/Portal 与 Contest 对接说明.md>)

## 人工管理

在 `contest/` 目录执行，把所需子命令接在 `.venv/bin/python -B -m service --config config/contest.json` 后面。

| 操作 | 子命令示例 |
| --- | --- |
| 查看对决 | `show duel-1` |
| 按原请求的双方顺序找回对决编号 | `ladder-submissions team001 team002 --since 2026-11-27T14:05:00+08:00` |
| 作废后重跑 | `void run-1.1 "平台故障"`，修复后执行 `retry run-1.1` |
| 补发交付未确认的任务 | `resend run-1.1` |
| 重新复核 | `recheck run-1.2` |
| 查看待裁定局和场次 | `rulings` |
| 裁定单局或整场 | `rule game run-1.1 team001 "裁定理由"` 或 `rule duel duel-1 team001 "裁定理由"` |

## 配置封存地图

主办方为初赛单循环和正式决赛各选定三张专门设计的地图，每张地图单独保存为 UTF-8 JSON 对象。封存时按原文件字节计算 SHA-256 并公布。以下命令在 `contest/` 目录计算示例地图的哈希。

```sh
sha256sum maps/prelim/P01.json
```

按比赛顺序把各文件的相对路径和哈希填入地图池的 `file` 和 `sha256`。例如，地图池配置位于 `config/` 时，`file` 填 `../maps/prelim/P01.json`。Contest 加载时校验文件哈希、赛段和地图规则。

根目录的 `.gitattributes` 为 `contest/maps/` 下的 JSON 保留原始字节，确保 Git 跨平台检出时哈希一致。

## 生成随机地图

`mapgen` 供天梯地图查看和测试使用。`--out` 导出 JSON 数组，`--count` 指定地图数量。

```sh
.venv/bin/python -B -m engine mapgen --seed 42 --stage prelim --count 3 --out maps.json
```

## 裁判与回放

使用初赛规则联调时，将两个终端的工作目录设为 `player-kit/`，使用 Contest 的虚拟环境从源码分别启动基准 Bot。决赛联调使用 Arena 提供的 `final` 运行环境。

```sh
# 终端 1
BOT_HOST=127.0.0.1 BOT_PORT=18101 \
  ../contest/.venv/bin/python -E -s -B -u -m sdk.python bots/bot001
# 终端 2
BOT_HOST=127.0.0.1 BOT_PORT=18102 \
  ../contest/.venv/bin/python -E -s -B -u -m sdk.python bots/bot002
```

在第三个终端进入 `contest/`。复制任务文件示例为 `task.json`，把两个 `bots[].url` 改为 Bot 的 HTTP 地址，再执行裁判和复核命令。 [任务文件示例](<../docs/Arena 与 Contest 对接说明.md#62-镜像交付与任务文件>)

```sh
.venv/bin/python -B -m judge --task task.json --out out
.venv/bin/python -B -m engine verify-replay out/replay.json --log-file out/log.jsonl
```

复核封存地图时，追加 `--map-file <地图原文件> --map-sha256 <公布的哈希>`。

## 构建裁判镜像

从仓库根目录构建。

```sh
docker build --platform linux/amd64 -f contest/judge/Dockerfile -t exposure-judge:local .
```

## 运行测试

在 `contest/` 目录执行。

```sh
.venv/bin/python -B -m tests --level L0
.venv/bin/python -B -m tests --level all
```
