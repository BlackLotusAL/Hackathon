# team009 · 回合超时 · Python

基于仓库的 Python SDK。Bot 正常就绪并完成初始化，策略每次处理回合时休眠 350 毫秒，超过 200 毫秒的响应时限。回放记为 `timeout`，累计 10 次触发技术判负。

使用 Python 3.9 或以上版本，无第三方依赖。在 `player-kit/` 目录运行下面的命令，预期退出码为 `1`。

```sh
python -m engine ../teams/team009
```
