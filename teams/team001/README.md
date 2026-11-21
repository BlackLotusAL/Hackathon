# team001 · 随机 · Python

本示例为 Python 随机策略，只使用标准库。相同地图和出生点可复现策略选择，设置 `ARENA_SEED` 可改变策略随机种子。

在项目的 `player-kit/` 目录使用 Python 3.9 或以上版本依次自检和试打。自检会试打 60 回合，试打命令会保存回放和日志。

```sh
python -m exposure check ../teams/team001
python -m exposure match ../teams/team001 bots/bot002 --map R42
```
