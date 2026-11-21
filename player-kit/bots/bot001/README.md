# bot001 随机

适合首次试打。每回合在可安全移动的方向中随机选择。随机种子取决于地图、出生点和环境变量 `ARENA_SEED`，改变该变量可换一组随机选择。

在包含 `exposure.py` 的材料包根目录，使用 Python 3.9 或以上版本运行。

```sh
python -m exposure match bots/my_bot_python bots/bot001 --map R42
```
