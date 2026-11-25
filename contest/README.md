# Contest

## 启动

在 Linux 的仓库根目录执行，使用 CPython 3.9.11。

```sh
cd contest
python3.9 -m venv .venv
```

按实际环境编辑 [config/contest.json](config/contest.json)。

```json
{
  "db": "../var/contest.sqlite",
  "archive_root": "/var/hackathon/archives",
  "maps": "maps.json",
  "phase": "prelim-ladder",
  "host": "0.0.0.0",
  "port": 8082,
  "ip_whitelist": null,
  "arena_url": "http://127.0.0.1:28080",
  "admin_user": "organizer",
  "admin_password": "contest"
}
```

将 `arena_url` 指向 Arena 的 HTTP 地址，将 `archive_root` 指向 Arena 发布对局归档的共享目录。Linux 部署使用 `/var/hackathon/archives`，Contest 和 Portal 以只读方式访问。初赛单循环和批量对战需要设置 `maps`。[共享归档](<../docs/Arena 与 Contest 对接说明.md#5-归档交付要求>) · [配置封存地图](#配置封存地图)

```sh
.venv/bin/python -B -m service --config config/contest.json serve
```

用配置中的账号密码登录。[打开管理后台](http://127.0.0.1:8082/admin/prelim)

## 配置文件

| `phase` | 用途 |
| --- | --- |
| `prelim-ladder` | 初赛天梯（默认） |
| `prelim` | 正式初赛单循环 |

切换 `phase` 后使用同一个 `db` 重启。遇到 `409 PHASE_BUSY`，等上一赛程的对决结算后重试。

- `db` 和 `maps` 的相对路径以配置文件所在目录为基准。`archive_root` 部署时使用共享目录的绝对路径，相对值以 `contest/` 为基准。联调时可在 `serve` 后加 `--archive-root <绝对路径>`，并将三端指向同一份存储。
- 用 `host`、`port` 设置监听地址和端口，用 `ip_whitelist` 填后台访问 IP 或 CIDR。

## 单场对决

1. 输入双方队伍编号，可用同一编号进行同队模拟。点击 **发起对决** 后，双方在同一张随机地图上换边打两局。
2. 在 **对决记录** 点击可播放的执行编号，自动从该局第 0 回合播放。同队模拟按蓝、红方统计赛果，初赛同时技术判负时双方各记一负。打满仍同指标的初赛模拟可在后台按蓝、红方裁定。
3. 点击 **结束模拟** 停止后续提交和查询，Arena 继续处理已受理的任务。

## 批量对战

1. 选 **批次类型**。**正式初赛** 需要 `phase=prelim`。**独立批次** 需填写批次编号（如 `load-trial-1`）并配置至少两队。
2. 用 **编号范围**（含起止编号）或 **手动添加**（用空格、逗号或换行分隔）配置队伍，点击 **开始对战**。每对队伍打三图六局。
3. 用 **查看批次** 切换记录，用 **结束批次** 停止独立批次。在 **待处理记录** 重跑已作废的执行，在 **人工裁定** 填写胜方和理由。
4. 在 **对局记录** 点击执行编号，播放当前批次已复核的单局回放。
5. 正式初赛结算后，点击 **确认赛果** 和 **下载初赛结果**，将 XLSX 导入 Portal。

## 接口自检

填写已启动的本机 Bot 地址（如 `http://127.0.0.1:18101`），再点击 **开始自检**。在 **自检报告** 查看逐项结果。[启动示例 Bot](#裁判与回放)

## Portal 联调

在初赛天梯赛程提交对决，用返回的 `duel_id` 查询状态和赛果。[Portal 对接说明](<../docs/Portal 与 Contest 对接说明.md>)

```sh
curl -i 'http://127.0.0.1:8082/contest/v1/duels' \
  -H 'Content-Type: application/json' \
  --data-binary '{"sides":["team001","team002"]}'

# 将 duel-1901 换成返回的 duel_id
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/status'
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/result'
```

## 人工管理

在 `contest/` 下，将子命令接在 `.venv/bin/python -B -m service --config config/contest.json` 后。

| 操作 | 子命令示例 |
| --- | --- |
| 查看对决 | `show duel-1` |
| 按原请求的双方顺序查找对决 | `ladder-submissions team001 team002 --since 2026-11-27T14:05:00+08:00` |
| 作废并重跑 | `void run-1.1 "平台故障"`，修复后执行 `retry run-1.1` |
| 补发未确认的任务 | `resend run-1.1` |
| 重新复核 | `recheck run-1.2` |
| 列出待裁定场次 | `rulings` |
| 裁定单局或整场 | `rule game run-1.1 team001 "裁定理由"` 或 `rule duel duel-1 team001 "裁定理由"` |

## 配置封存地图

1. 为初赛选三张 UTF-8 JSON 地图，计算并公布文件的 SHA-256。在 `contest/` 下可运行 `sha256sum maps/prelim/P01.json`。
2. 编辑 [config/maps.json](config/maps.json)，以 `prelim` 为键，值为按比赛顺序排列的三条 `{"file":"路径","sha256":"哈希"}` 的数组。`file` 相对于地图池文件所在目录，例如 `../maps/prelim/P01.json`。
3. 使用这份地图池时，将 `contest.json` 的 `maps` 设为 `maps.json`。

## 生成随机地图

在 `contest/` 下执行。

```sh
.venv/bin/python -B -m engine mapgen --seed 42 --count 3 --out maps.json
```

## 裁判与回放

在 `player-kit/` 的两个终端分别启动 Bot。

```sh
BOT_HOST=127.0.0.1 BOT_PORT=18101 \
  ../contest/.venv/bin/python -E -s -B -u -m sdk.python bots/bot001
BOT_HOST=127.0.0.1 BOT_PORT=18102 \
  ../contest/.venv/bin/python -E -s -B -u -m sdk.python bots/bot002
```

在 `contest/` 另开终端，将任务示例保存为 `task.json`，填写两个 `bots[].url` 后运行裁判和复核。[任务文件示例](<../docs/Arena 与 Contest 对接说明.md#62-镜像交付与任务文件>)

```sh
.venv/bin/python -B -m judge --task task.json --out out
.venv/bin/python -B -m engine verify-replay out/replay.json --log-file out/log.jsonl
```

复核封存地图时加上 `--map-file` 和 `--map-sha256`。回放参数 `station_period` 标识当前规则，取值为 100。

## 构建裁判镜像

在仓库根目录执行。

```sh
docker build --platform linux/amd64 -f contest/judge/Dockerfile -t exposure-judge:local .
```

## 运行测试

在 `contest/` 下运行全部 L0 核心用例。

```sh
.venv/bin/python -B -m tests
```
