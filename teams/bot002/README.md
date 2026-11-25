# bot002 贪心

Python 基准 Bot，优先从己方图形区闭合收益较高的矩形。每回合搜索预算为 80 毫秒，本地复现时可设置 `ARENA_NO_BUDGET=1` 关闭搜索预算。

在项目的 `player-kit/` 目录使用 Python 3.9 或以上版本运行下面的命令。

```sh
python -m engine ../teams/bot002 bot001
```
