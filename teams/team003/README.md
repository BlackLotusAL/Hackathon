# team003 · 搜索 · Java

本示例为 Java 搜索策略。准备 Python 3.9 或以上版本、Docker 和 Buildx。首次构建或缓存缺失时需要基础镜像和 Maven 依赖，可用 `ARENA_MAVEN_MIRROR_URL` 设置构建依赖源。

在项目的 `player-kit/` 目录依次自检和试打。工具自动构建并启动镜像，试打完成后保存回放和日志。

```sh
python -m exposure check ../teams/team003
python -m exposure match ../teams/team003 bots/bot002 --map R42
```
