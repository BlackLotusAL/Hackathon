# bot002 贪心

适合日常试打，也可复制本目录作为 Python Bot 的起点。策略优先避开切线威胁，随后考虑切对手轮廓和抢道具，从己方图形区边缘搜索收益较高的矩形路线。每回合搜索预算为 80 毫秒。本地复现时可设置 `ARENA_NO_BUDGET=1` 关闭搜索预算，并用 `--no-timing` 放宽练习回合时限。

在包含 `exposure.py` 的材料包根目录，使用 Python 3.9 或以上版本运行。

```sh
python -m exposure match bots/my_bot_python bots/bot002 --map R42
```
