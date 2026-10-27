# 系统说明

导航：[文档入口](../README.md) · [提案](../proposals/README.md) · [决策记录](14-decisions.md)

本目录描述 Maestro 在 `dev` 上**现在**是什么样：职责、不变量、关键流程，以及不显然的选择背后的原因。

## 文档分工

文档按寿命分开（U-33）：

| 文档 | 内容 | 写法 |
| --- | --- | --- |
| 系统说明（本目录 00～13、15） | 系统现状 | 直接改写，不追加；不写历史、不写“原设计 / 现状”分层；历史在 Git 中 |
| [决策记录](14-decisions.md) | 用户决定、技术取舍、冲突、上游依赖 | 只追加，每条几行 |
| [提案](../proposals/README.md) | 还没实现的设计与索引 | 开发前写；实现后并入系统说明并删除 |
| 提交说明与 PR 描述 | 改了什么、验证了什么、没覆盖什么 | 跟着 Git；原始证据放私有仓库 `maestro-site`（[N-23](../proposals/N-23-site-repository.md)） |
| [实施记录](records/README.md) | 重写期间（M1～M8）的记录 | 已冻结，不再新增 |
| [需求审计稿](requirements-review.md) | 重写的需求出处 | 已冻结 |

几条约定：

- **代码能表达的细节不写进文档**：表字段、接口清单、默认值以 `src/maestro/db/schema.py`、`openapi/maestro-v1.json`、`src/maestro/config.py` 与 `/api/v1/capabilities` 为准。文档写为什么、有哪些不变量、各部分怎么配合。
- **编号保持稳定**：代码注释以“NN section M”引用本目录，文件编号与 `## N.` 节号不改；节内可以重写。需求、场景与决定的编号（DS-01、E2E-RUN-01、F-05、U-17、D-020……）也保持稳定。
- **已知缺口**写在相关节的“已知限制”里；已规划的指向提案。
- 实践中发现系统说明有误或有更好的做法：改代码的同时改系统说明；涉及用户边界或重要取舍的，先在决策记录登记。

## 阅读入口

先读 [00 总览](00-overview.md)，再按任务读对应模块。

| 文档 | 主要内容 |
| --- | --- |
| [00 总览](00-overview.md) | 定位、提供什么、规模、架构和跨模块不变量 |
| [01 领域模型与数据](01-domain-model.md) | 术语、实体关系、数据约定、事务与并发 |
| [02 数据集与环境准备](02-datasets-preparation.md) | Hub 接入、版本准入、内容可得性、准备缓存、共享恢复 |
| [03 配置解析与附件](03-configuration-attachments.md) | 分层合并、内容快照、模型与 Harness、附件安装 |
| [04 实验方案与运行提交](04-plans-submission.md) | 校验、受理、批次、额度、重跑和来源关系 |
| [05 调度与执行](05-scheduling-execution.md) | 队列、资源、Worker、取消、重启和对账 |
| [06 结果与评分](06-results-scoring.md) | 错因、重试、零分、分母、覆盖率和多试次聚合 |
| [07 产物与导出](07-artifacts-export.md) | 上传、索引、下载、批量导出、保留、回收站 |
| 08 | 集合与比较尚未实现，见提案 [N-11](../proposals/N-11-collections-comparison.md) |
| [09 身份、权限与安全](09-identity-security.md) | 账户、PAT、权限、凭据和容器网络 |
| [10 API 与 SDK](10-api-sdk-cli.md) | 契约原则、错误、操作记录、幂等、能力发现 |
| [11 网页](11-web-ui.md) | 页面、关键流程、状态展示 |
| [12 部署运维](12-operations.md) | 安装、配置、启停、更新、备份和可观测性 |
| [13 测试与验收](13-testing-plan.md) | 测试分层、验收场景与故障场景 |
| [14 决策记录](14-decisions.md) | 用户决定、技术决定、冲突与上游依赖 |
| [15 技术栈与 Harbor 集成](15-stack-harbor.md) | 选型、Runner 与协议、Harbor 适配与升级风险 |
