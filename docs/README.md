# 文档导航

从[开发交接](HANDOFF.md)接手当前代码，再按任务选择下列入口。文档按用途和生命周期维护，当前系统说明与历史出处分开阅读。

| 需要了解什么 | 入口 | 维护职责 |
| --- | --- | --- |
| Agent 怎样在仓库工作 | [AGENTS.md](../AGENTS.md) | 阅读顺序、用户边界、文档与分支规则 |
| 当前代码与接手步骤 | [开发交接](HANDOFF.md) | 本分支的状态、最小接手步骤、环境陷阱与开发试用 |
| 系统怎样组成 | [系统架构](architecture/README.md)、[源码职责](architecture/source-layout.md) | 当前进程、边界、依赖方向与代码分工 |
| 模块怎样工作、为何如此 | [系统说明](design/README.md) | 当前行为、不变量、跨模块机制与取舍，直接改写 |
| 用户决定了什么 | [决策记录](design/14-decisions.md) | U 用户决定、D 技术决定、C 冲突、V 待验证依赖，追加记录 |
| 后续设计与历史取舍 | [提案索引](proposals/README.md) | 活动与暂缓提案；完成、取消和被取代的提案归档 |
| 怎样验证开发变更 | [开发验证](guides/testing.md) | 检查命令、环境隔离与 CI 范围；预期行为在[测试与验收](design/13-testing-plan.md) |
| 怎样协作和发布 | [开发与协作](guides/development-workflow.md)、[打包与发布](guides/releasing.md) | 工作树、分支集成与收尾；候选验收、版本、包仓库与发布 |
| 怎样安装和维护实例 | [部署维护](guides/deployment.md) | 发布包与 `maestroctl` 的操作，也作为包内的 `INSTALL.md` |

客户端开发与使用见[前端说明](../frontend/README.md)、[Python SDK](../sdk/README.md)。公开 API 的受控副本是 `openapi/maestro-v1.json`，运行中的服务在 `/api/v1/openapi.json` 导出，离线用 `maestro openapi`；网页类型与 SDK 模型同源生成。

## 历史出处

[需求审计稿](design/requirements-review.md)与重写期间的[实施记录](design/records/README.md)已冻结，保留原时点内容。旧决定不追补来源，已经删除的旧提案不恢复；之后的提案保留归档，新决定记录原话或可追溯对话出处（U-64）。

重写前的设计、归档、0.2.0 发布说明和旧验收标准已删除，可在 Git 历史（`9ff43b5` 及更早）中查阅，不成为新增约束。部署现场说明与原始证据放私有仓库 `auua/maestro-site`，CI 的输出看 Actions 运行日志，均不复制到公开文档。
