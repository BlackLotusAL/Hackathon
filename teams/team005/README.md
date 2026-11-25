# team005 · 非法指令 · Python

基于仓库的 Python SDK。Bot 正常就绪并完成初始化，每回合返回无效方向 `X`。SDK 返回 HTTP `500`，回放记为 `invalid`，累计 10 次触发技术判负。

使用 Python 3.9 或以上版本，无第三方依赖。在 `player-kit/` 目录运行下面的命令，预期退出码为 `1`。

```sh
python -m engine ../teams/team005 bot002
```
