# team003 · 轮廓回收 · Python

基于仓库的 Python SDK。在己方图形区内优先向外移动，离开后优先返回己方图形区以闭合轮廓。优先级相同时保持原方向。

使用 Python 3.9 或以上版本，无第三方依赖。在 `player-kit/` 目录运行下面的命令，预期完成 600 回合，退出码为 `0`。

```sh
python -m engine ../teams/team003 bot002
```
