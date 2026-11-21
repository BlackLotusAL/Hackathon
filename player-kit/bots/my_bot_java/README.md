# Java 策略模板

在 `src/main/java/arena/bot/Strategy.java` 中实现策略。新增依赖时，在 `pom.xml` 中声明。

准备 Python 3.9 或以上版本、Docker 和 Buildx。构建使用 Maven 和 JDK 17，首次构建需要基础镜像及 Maven 依赖。可用 `ARENA_MAVEN_MIRROR_URL` 指定依赖源。在包含 `exposure.py` 的材料包根目录运行自检。

```sh
python -m exposure check bots/my_bot_java
```

提交时将模板内容放到本队仓库根目录，保留 `bot.json`、`README.md`、`pom.xml` 和 `src/main/java/arena/bot/Strategy.java`。在 README 说明策略、依赖和代码来源，以实际提交目录自检后推送到 `main`。
