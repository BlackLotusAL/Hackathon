# bot002 贪心

适合用来测试策略，也可复制本目录作为 Python Bot 的起点。策略优先避开切线威胁，随后考虑切对手轮廓和抢道具，从己方图形区边缘搜索收益较高的矩形路线。每回合搜索预算为 80 毫秒。

在材料包根目录运行下面的命令。

```sh
python -m engine bots/my_bot_python --opponent bot002
```
