# team006 · 追踪订单 · Java

基于仓库的 Java SDK 和 Maven 模板。有当前订单时优先靠近订单区域，距离相同时优先走向己方图形区以外的格子。无订单时沿用这一选格偏好。

使用 Python 3.9 或以上版本、JDK 17 和 Maven。Jackson 依赖见 `pom.xml`。在 `player-kit/` 目录运行下面的命令，预期完成 600 回合，退出码为 `0`。

```sh
python -m engine ../teams/team006
```
