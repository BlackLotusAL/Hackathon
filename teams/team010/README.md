# team010 · 对局中退出 · Python

基于仓库的 Python SDK。Bot 正常就绪并响应前两回合，从第 3 回合请求起以退出码 `1` 结束整个进程。预期对局因 `crash` 技术判负。

使用 Python 3.9 或以上版本，无第三方依赖。在 `player-kit/` 目录运行下面的命令，预期退出码为 `1`。

```sh
python -m engine ../teams/team010
```
