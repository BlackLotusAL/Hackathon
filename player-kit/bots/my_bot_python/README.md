# Python 策略模板

在 `strategy.py` 中实现 `Strategy.act`。将额外依赖写入 `requirements.txt`，无额外依赖时可留空。

规则常量来自 `game_core.rules`。`InitState` 提供开局地图和出生点，`TurnState` 提供当前局面及双方状态。每回合从 `Strategy.act` 返回 `U`、`D`、`L`、`R` 之一，加速时可用 `+` 后缀。

使用 Python 3.9 或以上版本，在包含 `exposure.py` 的材料包根目录运行自检。额外依赖需安装在同一个 Python 环境。

```sh
python -m exposure check bots/my_bot_python
```

提交时将模板内容放到本队仓库根目录，保留 `bot.json`、`README.md`、`strategy.py`、`requirements.txt`。在 README 说明策略、依赖和代码来源，以实际提交目录自检后推送到 `main`。
