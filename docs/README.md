# 文档导航

文档按寿命分开：系统说明只写现状，提案写待实现的设计，决策记录只记决定（U-33）。

## 系统与规划

- [系统说明](design/README.md)：Maestro 现在是什么样，以及文档分工的规则。
- [提案](proposals/README.md)：下一步做什么，按优先级排列的索引与提案模板。
- [决策记录](design/14-decisions.md)：用户决定、技术决定、待确认冲突与上游依赖。
- [测试与验收](design/13-testing-plan.md)：测试分层、验收与故障场景。
- 已冻结的出处：[需求审计稿](design/requirements-review.md)、重写期间的[实施记录](design/records/README.md)。

## 代码

- [源码职责](architecture/source-layout.md)：`maestro` 包、Runner、SDK 与安装工具的分工。
- [前端开发](../frontend/README.md)、[Python SDK](../sdk/README.md)。
- 公开 API 的受控副本是 `openapi/maestro-v1.json`（运行中的服务在 `/api/v1/openapi.json`，离线用 `maestro openapi`）；网页类型与 SDK 模型从它生成。

基线的 Web 服务指南、机器 API 文档、研究客户端 skill 与附件说明已删除，需要时查 `9ff43b5`。

## 部署

- [部署与维护](guides/deployment.md)：用发布包与 `maestroctl` 安装、启停、备份恢复、更新和卸载（也作为发布包里的 `INSTALL.md`）。

## 开发协作

- [开发交接](HANDOFF.md)：现状、代码位置和验证入口。
- [分支、协作与发布规则](guides/development-workflow.md)：独立工作树、`dev` 集成与固定版本发布。

重写前的设计、归档、0.2.0 发布说明和旧验收标准已删除，可在 Git 历史（`9ff43b5` 及更早）中查阅，不成为约束。现场说明与原始证据放私有仓库 `auua/maestro-site`，不进入本仓库。
