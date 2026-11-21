# Portal 与 Contest 对接说明

Portal 通过 HTTP 下达天梯挑战和决赛对决并查询结果。初赛单循环由主办方使用 XLSX 交接名单和赛果。单局明细、回放和日志从共享归档读取。

## 1. 接口地址

Portal 和 Contest 部署在同一台 Linux 服务器，服务间调用使用本机 HTTP，无需服务间令牌或认证头。联调示例使用 `http://127.0.0.1:8082`，部署时以 Contest 的监听地址和端口为准。共享归档按第 5.1 节配置为只读。面向选手的页面与业务接口使用公司 SSO，并按角色、队伍关系和赛事阶段授权。

HTTP JSON 和归档文本使用 UTF-8。POST 发送 `Content-Type: application/json`，请求体上限为 64 KiB。错误响应的 JSON 包含稳定的 `code`，可附 `message`。时间采用带 `+08:00` 的 ISO 8601 格式，比较和排序时保留微秒精度。

| 用途 | 方法与路径 | 成功响应 |
| --- | --- | --- |
| 提交对决 | `POST /contest/v1/duels` | `202 Accepted`，空响应体 |
| 查询对战状态 | `GET /contest/v1/duels/{duel_id}/status` | `200 OK`，状态 JSON |
| 查询整场结算 | `GET /contest/v1/duels/{duel_id}/result` | `200 OK`，赛果 JSON |

两条查询均只传路径中的 `duel_id`，响应带 `Cache-Control: no-store`。

## 2. 提交天梯挑战或正式对决

### 2.1 请求字段

Portal 校验报名资格和赛事阶段，将双方映射为队伍或基准 Bot 编号，为 HTTP 提交生成 `duel_id`，并在发送前与原请求一起保存。初赛单循环的 `duel_id` 由 Contest 编排时生成，Portal 导入工作簿时保留原值。

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| `duel_id` | string | 必填，整场对决的唯一编号，匹配 `duel-[A-Za-z0-9_-]+` |
| `rules` | string | 必填，`prelim` 为初赛规则，`final` 为决赛规则 |
| `sides` | string[2] | 必填，两个不同的参赛方编号。队伍编号为 `team` 加三位数字，基准编号为 `bot001`、`bot002`、`bot003`。下标 0、1 对应每张地图首局的出生点 |
| `map_seed` | integer 或 string | 可选，按下表选择正式地图池或天梯地图 |

| `map_seed` | 执行方式 |
| --- | --- |
| 省略 | 使用 `rules` 对应的正式地图池 |
| `prelim` 或 `final` | 使用对应的正式地图池，须与 `rules` 一致 |
| 整数 | 天梯使用指定的 JSON 整数种子 |
| `random` | 天梯由 Contest 生成并固定实际地图种子 |

天梯仅在对应赛段开放时接受新挑战。发起队伍须已报名且 `main` 分支上有代码，对手为另一支具备天梯资格的队伍或一个基准 Bot。每队同时最多有一场由本队发起、仍未结束的挑战。受理后通知被挑战方，被挑战方无需在线或确认。天梯每场使用一张地图打两局，Portal 随机确定首局出生点并保存为 `sides`。

正式对决的双方均为参赛队伍，每场按已封存的三张地图依次各打两局。Portal 为每个可能的决赛对阵随机确定并记录首局出生点。Contest 在每张地图的首局使用 `sides`，第 2 局交换两方。

### 2.2 天梯提交示例

将请求保存为 `duel-1901.json`，使用初赛规则和指定种子。

```json
{
  "duel_id": "duel-1901",
  "rules": "prelim",
  "sides": ["team001", "team002"],
  "map_seed": 42
}
```

```sh
curl -i -X POST 'http://127.0.0.1:8082/contest/v1/duels' \
  -H 'Content-Type: application/json' \
  --data-binary @duel-1901.json
```

### 2.3 决赛提交示例

将请求保存为 `duel-1902.json`，沿用上一节的命令并指定本例文件。

```json
{
  "duel_id": "duel-1902",
  "rules": "final",
  "sides": ["team001", "team003"],
  "map_seed": "final"
}
```

### 2.4 受理、错误与补发

| 响应 | 含义 |
| --- | --- |
| `202` | 已持久化受理。以相同 `duel_id` 重复提交同一请求时，返回相同状态码并复用已有计划 |
| `400 INVALID_JSON` 或 `INVALID_DUEL` | 请求 JSON、编号、规则赛段、双方或地图选择无效 |
| `409 DUEL_CONFLICT` | 同一 `duel_id` 已关联不同的提交内容 |
| `503 MAP_POOL_UNAVAILABLE` | Contest 尚未配置所选赛段的正式地图池 |

Portal 收到 `202` 后向业务调用方返回已保存的 `duel_id`，开始查询结算。提交响应不确定时标记交付未确认，先按原编号查询，必要时按原编号和原请求补发，保留原请求的所有字段及省略方式。已受理的对决发生平台故障时，Portal 继续查询原 `duel_id`。主办方在 Contest 按单局发起重跑，生效的尝试会出现在最终的 `runs` 中。

### 2.5 决赛对阵与推进

初赛排名公布后，从具备资格的队伍中按排名取前 16 支。晋级名单最终确认前若有人退赛或被取消资格，按初赛排名顺延递补。最终名单按初赛名次确定 1–16 号种子。

| 半区 | 首轮对阵及晋级路径 |
| --- | --- |
| 上半区 | （1 对 16、8 对 9）和（4 对 13、5 对 12） |
| 下半区 | （2 对 15、7 对 10）和（3 对 14、6 对 11） |

同一括号内的两场胜方在四分之一决赛相遇。同半区的四分之一决赛胜方进入半决赛。两场半决赛胜方争夺冠军，仍具备资格的明确负方参加季军赛。

名单确认后发生退赛或取消资格时，Portal 记录队伍资格和对应场次，并将队伍编号及 `duel_id` 提供给主办方。当前场次已结算时，保留原结果，等该队下一场的双方确定后办理。Portal 使用第 2.3 节的接口提交已确定双方的正式对决，主办方在 Contest 对已受理的场次选择一方或双方录入资格判负。Portal 通过原结果接口取得 `winner`、`forfeited` 和 `reason`，按 `winner` 推进有资格的胜方，并将 `forfeited` 中的队伍排除在后续对阵之外。 [Contest 决赛资格判负](<Contest 子系统需求设计.md#42-决赛资格判负>)

双败时该场的后续晋级位置留空，发生在半决赛时对应的季军赛位置也留空。后续场次只有一支具备资格的队伍时，该队直接取得胜方对应的晋级资格或名次。双方缺位时该场留空。因缺位未开打的场次不计局数。冠军赛和季军赛的胜负分别确定第 1–2 名和第 3–4 名，无法确定的名次留空。四分之一决赛出局者并列第 5 名，首轮出局者并列第 9 名。

## 3. 查询状态和结算结果

已受理的对决在待定与已结算时均返回 `200`。编号格式无效返回 `400 INVALID_DUEL`，编号不存在返回 `404 DUEL_NOT_FOUND`。

### 3.1 对战状态

```sh
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/status'
```

```json
{"duel_id": "duel-1901", "state": "in_progress"}
```

`in_progress` 涵盖排队、执行、重跑、复核及等待裁定。整场结算持久化后，状态为 `finished`。

### 3.2 整场结果与对局明细

```sh
curl -i 'http://127.0.0.1:8082/contest/v1/duels/duel-1901/result'
```

待定时返回原 `duel_id`，按参赛方编号返回 `side_results`，`result` 为 `null`。

```json
{
  "duel_id": "duel-1901",
  "side_results": {"team001": "pending", "team002": "pending"},
  "result": null
}
```

| 整场结算 | `side_results` | `result.outcome` | `result.winner` |
| --- | --- | --- | --- |
| 单方获胜 | 胜方 `win`，负方 `loss` | `win` | 胜方编号 |
| 天梯平局 | 双方 `draw` | `draw` | `null` |
| 整场双败 | 双方 `loss` | `double_loss` | `null` |

同一场天梯的第一局最初使用 `run-7001.1`，因整局超时作废，最终生效的是 `run-7001.2`。第二局换边，首次执行 `run-7002.1` 生效。两局得分、奖励和领先回合数均相同，`team001` 都凭较短的累计响应时间获胜。

```json
{
  "duel_id": "duel-1901",
  "side_results": {"team001": "win", "team002": "loss"},
  "result": {
    "outcome": "win",
    "winner": "team001",
    "games_won": {"team001": 2, "team002": 0},
    "games_lost": {"team001": 0, "team002": 2},
    "runs": ["run-7001.2", "run-7002.1"],
    "games": [
      {"ordinal": 1, "run_id": "run-7001.2", "winner": "team001", "score": {"team001": 9, "team002": 9}},
      {"ordinal": 2, "run_id": "run-7002.1", "winner": "team001", "score": {"team001": 9, "team002": 9}}
    ],
    "started_at": "2026-11-27T14:05:00+08:00",
    "ended_at": "2026-11-27T14:10:00.000001+08:00",
    "previous_duel_ids": []
  }
}
```

| `result` 字段 | 含义 |
| --- | --- |
| `outcome`、`winner` | 整场结果与胜方。胜方为队伍或基准 Bot 编号，平局或双败为 `null` |
| `games_won`、`games_lost` | 按参赛方编号记录的登记胜负局数，包含整场判罚 |
| `runs`、`games` | 均按预定局序排列，以完整 `run_id` 关联。每项 `games` 含原计划局序 `ordinal`、`run_id`、生效胜方 `winner` 和以参赛方编号为键的 `score`。经人工裁定的单局另附 `reason`。只列生效单局，局序可以有缺号，无生效单局时两者均为空数组 |
| `tiebreak` | 正式对决按累计指标决胜时提供，取 `score`、`bonus`、`lead_turns` 或 `time_us` |
| `build_run_id` | 构建判罚所用尝试的完整 `run_id`，路径见第 5.1 节。这次尝试只作为构建证据，已有生效单局仍由 `runs` 列出 |
| `forfeited` | 决赛资格判负时提供，按编号排序，包含被判负的一支或两支队伍。半决赛被列入此字段的队伍缺席季军赛 |
| `reason` | 整场人工裁定或其他主办方判定的理由 |
| `started_at` | 天梯结果必填。Contest 从该场归档清单的 `duel_started_at` 取得，Portal 直接使用响应中的值计分 |
| `ended_at` | 天梯结果必填，Contest 的结算确认时间，按确认顺序递增 |
| `previous_duel_ids` | 天梯结果必填，按确认顺序列出同赛段双方各自上一场已确认的挑战，去重后最多两项 |

按 `games_won` 和 `games_lost` 展示登记局数。正式对决先比胜局数。双方各胜 3 局时，依次比较六局累计的 `score`、`bonus`、`lead_turns`、`time_us`，前三项较高者优先，耗时较少者优先。四项全同时，主办方录入裁定后，Portal 使用 `winner` 推进赛程，保留 3∶3 的局数并展示 `reason`。提供 `tiebreak` 时，从 `runs` 对应的单局清单按第 5.2 节归队累加，展示四项累计指标并标示决胜项。

只有一方因自身提交构建失败时，该方按预定局数登记负局，对手登记同等胜局。双方均构建失败时，各登记全部预定局数为负局。预定局数为天梯 2 局或正式对决 6 局。已有的生效单局记录继续展示，整场登记局数以结算字段为准。

决赛资格判负按 6 局登记。以第 2.3 节的对决为例，`team003` 确认退赛，录入时尚无生效单局。状态查询返回 `finished`，结果如下。双方均判负时，`outcome` 为 `double_loss`、`winner` 为 `null`、双方登记 0 胜 6 负。

```json
{
  "duel_id": "duel-1902",
  "side_results": {"team001": "win", "team003": "loss"},
  "result": {
    "outcome": "win",
    "winner": "team001",
    "games_won": {"team001": 6, "team003": 0},
    "games_lost": {"team001": 0, "team003": 6},
    "runs": [],
    "games": [],
    "forfeited": ["team003"],
    "reason": "队伍确认退赛"
  }
}
```

### 3.3 Portal 轮询与入库

1. Portal 后台轮询结果接口。`result` 为 `null` 或查询暂时失败时，稍后按原编号重试。只展示进度时可查询状态接口。
2. 收到天梯结算时，先按 `previous_duel_ids` 补查并应用缺失的前序结果，再按第 3.4 节应用当前结果。
3. 按 `duel_id` 保存结算结果。计分或晋级与标记该编号已应用须在同一事务中完成，后续查询以该记录去重。

### 3.4 天梯计分与榜单

初赛、决赛天梯独立计分，各赛段开放时初始分为 1200。分别从 `started_at` 之前已结算的同赛段结果中取双方开局分。对一方，设开局分为 `R`、对手开局分为 `R'`，本场成绩 `S = games_won[本方] / 2`。各赢一局时双方的 `S` 均为 0.5，构建双败时均为 0。

`E = 1 / (1 + 10^((R' - R) / 400))`，新分数为 `R + 20 × (S - E)`。按 `ended_at` 确认顺序应用同一队伍的结果，保存小数分，榜单显示四舍五入后的整数。

队伍完成首场挑战后入榜，依次按实际分数从高到低、已完成挑战次数从多到少、达到该分数的时间从早到晚排序。基准 Bot 同样计分，在榜单上标为「基准」，不占名次。关闭天梯时停止接受和启动新挑战，已开始的挑战正常完成并计分。更新代码提交保留已有天梯分。

## 4. 初赛 XLSX 往返格式

### 4.1 文件交接流程

1. 初赛提交截止后，Portal 以截止时已报名且代码仓库的 `main` 分支上有代码的队伍生成名单。主办方导出并留存 `队伍名单.xlsx`，在 Contest 导入后编排队伍间的单循环。
2. 主办方在 Contest 确认赛果后，导出含「排名」「对决」「对局」三张工作表的 `初赛结果.xlsx`。
3. Portal 校验并导入整本工作簿，主办方另行确认公示。

文件使用 `.xlsx` 工作簿和固定值单元格。编号用文本，名次、局数与局序用整数，缺失值以空单元格表示。工作表名及表头使用下列名称。

### 4.2 队伍名单

`队伍名单.xlsx` 的工作表名为「队伍」。

| 队伍编号 | 队名 |
| --- | --- |
| team001 | 微光 |
| team002 | 曝光 |
| team003 | 领地 |

名单至少包含一支参赛队伍。队伍编号必填且唯一，队名可省略或留空，仅供人工核对。Contest 忽略空白行，拒绝编号为空但有队名的行，以及公式、错误单元格、无效或重复编号。上传上限为 16 MiB。

Portal 按示例的中文表头导出。编排后重新交付同一名单时，保留原队伍、队名和行序。

### 4.3 初赛排名

`初赛结果.xlsx` 的「排名」工作表使用下列表头及顺序。分别按队伍汇总各场登记的胜负局数，依次按胜局数降序、负局数升序、队伍编号升序排列。名次从 1 连续递增。本节与后两节共用一个三队示例。

| 名次 | 队伍编号 | 胜局数 | 负局数 |
| --- | --- | --- | --- |
| 1 | team001 | 5 | 7 |
| 2 | team003 | 5 | 7 |
| 3 | team002 | 2 | 10 |

### 4.4 初赛对决

「对决」工作表每场一行，列顺序如下。

| 列 | 表头 | 含义 |
| --- | --- | --- |
| A | `duel_id` | 原对决编号 |
| B | 队伍1编号 | 原始 `sides[0]` |
| C | 队伍2编号 | 原始 `sides[1]` |
| D | 队伍1胜局数 | 队伍1登记的胜局数 |
| E | 队伍1负局数 | 队伍1登记的负局数 |
| F | 队伍2胜局数 | 队伍2登记的胜局数 |
| G | 队伍2负局数 | 队伍2登记的负局数 |
| H | 整场结果 | `win` 或 `double_loss` |
| I | 胜方 | 胜方队伍编号，双败时留空 |
| J | 判定原因 | 供展示的原因，例如「胜局数」「构建失败」「构建双败」「退赛」、四项累计指标代码或「人工裁定（录入的理由）」 |
| K | `build_run_id` | 构建失败尝试的完整执行编号，其他情况留空 |

| duel_id | 队伍1编号 | 队伍2编号 | 队伍1胜局数 | 队伍1负局数 | 队伍2胜局数 | 队伍2负局数 | 整场结果 | 胜方 | 判定原因 | build_run_id |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| duel-prelim-12 | team001 | team002 | 0 | 6 | 0 | 6 | double_loss |  | 构建双败 | run-1.1 |
| duel-prelim-13 | team001 | team003 | 5 | 1 | 1 | 5 | win | team001 | 胜局数 |  |
| duel-prelim-23 | team002 | team003 | 2 | 4 | 4 | 2 | win | team003 | 胜局数 |  |

Portal 按双方列还原 `games_won` 和 `games_lost`，空白胜方还原为 `null`，构建判定编号为空则省略 `build_run_id`。判定原因为四项累计指标代码时，可用作 `tiebreak`。

### 4.5 初赛单局

「对局」工作表每个生效单局一行。「预定局序」使用该场原计划中的序号，范围为 1–6。「胜方」为 Contest 确认的参赛方编号，经人工裁定的单局填写「裁定理由」。构建失败的尝试仅以 `build_run_id` 登记在「对决」表。

| duel_id | 预定局序 | run_id | 胜方 | 裁定理由 |
| --- | --- | --- | --- | --- |
| duel-prelim-13 | 1 | run-7.2 | team001 |  |
| duel-prelim-13 | 2 | run-8.1 | team001 |  |
| duel-prelim-13 | 3 | run-9.1 | team001 |  |
| duel-prelim-13 | 4 | run-10.1 | team001 |  |
| duel-prelim-13 | 5 | run-11.1 | team001 |  |
| duel-prelim-13 | 6 | run-12.1 | team003 |  |
| duel-prelim-23 | 1 | run-13.1 | team003 |  |
| duel-prelim-23 | 2 | run-14.1 | team003 |  |
| duel-prelim-23 | 3 | run-15.1 | team003 |  |
| duel-prelim-23 | 4 | run-16.1 | team003 |  |
| duel-prelim-23 | 5 | run-17.1 | team002 |  |
| duel-prelim-23 | 6 | run-18.1 | team002 |  |

Portal 按以下要求校验并导入整本工作簿。

1. 核对工作表名和表头、队伍归属、对决编号唯一性、双方不同、整数列与结果枚举。
2. 每条「对局」须关联已存在的「对决」，「胜方」须为该场的参赛队伍。`(duel_id, 预定局序)` 和 `run_id` 在表内分别唯一。
3. 按 `duel_id` 分组，将 `run_id` 按「预定局序」数值升序还原为 `runs`，并记录各局的生效胜方和裁定理由。只使用表中登记的生效单局，允许少于六局或为空。
4. 核对「排名」与「对决」登记胜负局数的汇总及排序。三表全部通过后作为同一批次入库。重复导入相同文件时复用记录，批次内容冲突时交主办方核对。

## 5. 归档读取与公示

### 5.1 目录与执行编号

Arena 写入共享归档，Contest 和 Portal 以只读方式访问。三端挂载路径可以不同，指向同一份存储。以下相对路径均以 Portal 的 `archive_root` 为基准。项目位于 `/srv/hackathon` 时，正式环境示例为 `/srv/hackathon/contest/var/archives/contest/`，本地联调示例为 `/srv/hackathon/contest/var/archives/contest-local/`。

`duel_id` 标识整场，`ordinal` 是从 1 起的场内预定局序。`run_id` 由 Contest 为每次执行分配，匹配 `run-[0-9]+\.[1-9][0-9]*`。例如 `run-7001.2` 表示局号 7001 的第 2 次尝试，局号在同一归档中唯一。Portal 使用响应或工作簿给出的完整 `run_id` 拼接路径和设置缓存键。

| 根目录下的相对路径 | 用途 |
| --- | --- |
| `runs/<run_id>/match.json` | 单局清单或构建失败清单 |
| `runs/<run_id>/replay.json` | 完成的单局回放。Portal 按 `runs` 列出的生效尝试读取 |
| `runs/<run_id>/p0.stderr.log`、`runs/<run_id>/p1.stderr.log` | 出生点 0、1 的 Bot 输出，存在时可按队授权读取 |
| `runs/<run_id>/log.jsonl` | 裁判观测，供平台复核 |
| `builds/<team_id>/<run_id>.log` | 构建日志，按实际产生情况保留。展示构建判罚证据时以 `build_run_id` 填入路径中的 `<run_id>` |

第 4.4 节的构建双败示例中，`build_run_id` 为 `run-1.1`。从 `runs/run-1.1/match.json` 读取构建结论。队伍 `team001` 的构建日志若存在，位于 `builds/team001/run-1.1.log`。

### 5.2 单局清单

| 字段 | 读取方式 |
| --- | --- |
| `bots` | 按当局出生点排列的两项。每项的 `team_id` 对应参赛方，`commit` 记录队伍的固定提交，基准 Bot 可省略提交号 |
| `result.engine_version`、`result.map`、`result.map_sha256` | 实际引擎版本、地图编号和哈希。随机地图编号形如 `R42`，其中 42 为实际种子 |
| `result.winner`、`result.reason` | 胜方出生点 0 或 1（未决胜时为 `null`），以及裁判的单局终止原因，例如 `time` |
| `result.score`、`result.bonus`、`result.lead_turns`、`result.time_us` | 按出生点排列的得分、累计订单奖励、领先回合数和累计响应时间。耗时以整数微秒保存，可按界面需要换算为毫秒 |
| `result.turns` | 可选，已经结算的回合数 |
| `duel_started_at` | 天梯清单必有该场固定的开始时间，正式比赛可以省略 |

完成清单中的胜方、指标数组和 Bot 日志均按当局出生点解释。用 `bots[出生点].team_id` 换算为参赛方。以第 3.2 节的天梯为例，`team001` 在 `run-7001.2` 的出生点为 0，其日志路径为 `runs/run-7001.2/p0.stderr.log`。换边后它在 `run-7002.1` 的出生点为 1，日志路径为 `runs/run-7002.1/p1.stderr.log`。

Portal 展示单局赛果时，以查询响应 `games[].winner` 或初赛「对局」表的「胜方」为准。人工裁定后的归档仍记录裁判观测到的原始结论，可能显示 `result.winner: null`。Portal 同时展示 Contest 给出的裁定理由。

构建失败清单只包含 `bots` 和适用的 `duel_started_at`。双方都有 `bots[].state`，取 `succeeded` 或 `failed`，至少一方为 `failed`。

### 5.3 回放读取

`replay.json` 的 `format` 为 `arena-replay/2`。`stage` 标识赛段，`players` 为按出生点排列的显示名，参赛方身份以清单中的 `bots[].team_id` 为准。`map` 包含尺寸 `size`、地形行 `terrain`、校准区中心 `calib`、订单区域 `orders` 和道具刷新点 `boost_points`。坐标原点为左上角，`x` 向右、`y` 向下。`terrain` 中 `.` 为普通格，`#` 为缺陷，`*` 为关键图形区。

1. 用 `init.owner` 和 `init.contour` 初始化归属和轮廓。两者均为按行展开的字符串，`.` 表示无主或无轮廓，`0` 和 `1` 表示出生点。决赛的 `init.latent` 记录潜像倒计时。
2. 按 `frames[].turn` 播放。每项 `d` 是格子变化列表，每条为 `[格子编号, 归属, 轮廓]`，决赛附加潜像值。格子编号为 `y × size + x`。
3. `init` 和 `frames` 中的 `beams` 提供按出生点排列的束斑状态，包含坐标 `x`、`y`、方向 `dir`、在场状态 `alive`、剩余校准回合 `restart_in` 和剩余加速回合 `boost_left`。`score` 和 `bonus` 为当前得分与奖励。帧中的 `cmds`、`order`、`boost`、`boost_next` 和 `events` 可用于展示命令、订单、道具与事件。`events[].t` 为事件类型，可选的 `ms` 为显示用的回合耗时。终局信息取自 `result`。

### 5.4 可见性与公示

| 内容 | 开放范围与时机 |
| --- | --- |
| 天梯榜 | 对全体已报名队伍展示队名、天梯分、已完成挑战次数和胜率 |
| 天梯回放 | 仅向参与该场挑战的队伍开放 |
| 正式赛果和有效单局回放 | 初赛单循环全部完成并确认后公示排名、全部对决结果及有效单局回放。正式地图赛前公布哈希，对应赛段结束后随回放公开地图内容 |
| 提交号、Bot 输出和构建日志 | 按归属队伍授权，队伍仅能查看本队内容 |

回放包含完整地图，下载和播放入口均按上述时机授权。Portal 从清单中选取可显示的字段，通过鉴权后的接口提供回放与日志。
