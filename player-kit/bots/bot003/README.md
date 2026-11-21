# bot003 搜索

适合进阶对战练习。在贪心策略的基础上推演双方到达候选路线的时间，并考虑更大的闭合区域。每回合推演预算为 100 毫秒，到时使用已找到的路线。

从材料包根目录试打。

```sh
python -m engine bots/my_bot_python --opponent bot003
```
