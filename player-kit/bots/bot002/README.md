# bot002 贪心

适合日常试打，也可复制本目录作为 Python Bot 的起点。策略优先避开切线威胁，随后考虑切对手轮廓和抢道具，从己方图形区边缘搜索收益较高的矩形路线。每回合搜索预算为 80 毫秒。

从材料包根目录试打。

```sh
python -m engine bots/my_bot_python --opponent bot002
```
