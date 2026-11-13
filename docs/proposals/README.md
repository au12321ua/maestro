# 提案

导航：[文档入口](../README.md) · [系统说明](../design/README.md) · [决策记录](../design/14-decisions.md)

活动提案写“要改成什么、为什么”，归档提案保留当时的方案、取舍与结果。系统现在的样子写在[系统说明](../design/README.md)里，决定写在[决策记录](../design/14-decisions.md)里。

## 生命周期

1. **想法**：本页索引中的一行。只有一句话，不必有文件。
2. **设计中**：开始认真讨论时，按下面的模板写成 `N-<编号>-<英文短名>.md`。
3. **已确认**：用户确认范围与待定问题后，才能按它实现（用户的决定同时写进决策记录第 0 节）。
4. **实施中 → 完成**：实现合入 `dev` 的同一个 PR 中，把结论改写进受影响的系统说明，提案标记完成并移入 `archive/`，本页保留状态与归档链接。实施证据写在提交说明与 PR 描述里。

5. **暂缓**：正文保留在当前目录，正文与索引都标记暂缓；重新推进时先核对现状与授权范围。
6. **取消 / 被取代**：正文移入 `archive/`，保留原因；被取代的注明替代提案，本页保留状态与链接。

提案编号不复用，沿用本项目的 N 编号与中文状态。归档规则从本次整理起执行；已经删除的旧提案不恢复，已有完成项保留摘要（U-64）。

## 索引

按 U-57 的优先级排列。“已定”指用户已明确的选择，其余是建议。

### 由用户测试后再推进

| 编号 | 提案 | 状态 | 要点 |
| --- | --- | --- | --- |
| N-22 | 已知遗留 | 想法 | 共享构建不计入主机 CPU / 内存；10 试次并行压测未做；“停用但仍在使用的对象”未进待处理；现场主机的存储驱动与镜像共用情况未核对。用户亲自测试后再推进（U-57） |

### 来自实验的改进

来自 mesh2cad 附件实验。设计已按讨论定稿（U-99、U-100），已确认实施（U-101）。

| 编号 | 提案 | 状态 | 要点 |
| --- | --- | --- | --- |
| N-39 | [skill 补上数据集上传与任务环境](archive/N-39-skill-datasets-environments.md) | 完成 | 补 SDK 数据集参考页，讲清上传权限、平台构建镜像、来源与版本边界及跨版本比较限制；两个 skill 共用入口 |
| N-40 | [Oracle 运行带附件](archive/N-40-oracle-attachments.md) | 完成 | Oracle 先安装附件再跑参考解，复用安装器与错误语义；仍是测试模式。已定：Oracle 下不做 Harness 兼容检查，有附件时 setup 使用安装网络 |
| N-41 | [给已有提交评分](archive/N-41-submission-runs.md) | 完成 | 附件增加“待评提交”类别，由脚本按任务名放好文件；新执行方式“提交评分”装好后直接跑 verifier，不调模型、不跑参考解，不参与比较。平台不检查它是否真的起到答案的作用。依赖 N-40 |

### 产品方向：开源的自托管平台与公开评测结果（U-31）

| 编号 | 提案 | 状态 | 要点 |
| --- | --- | --- | --- |
| N-17 | 兼容承诺的起点 | 想法（不急，目前只有一名使用者，U-57） | 0.x 期间仍可不兼容，但数据库变更从 0.3.0 起只追加迁移（已定，U-34），发布说明列出破坏性变更。自第一个面向外部用户的版本起：数据库自动迁移、公开 API 与 SDK 按语义化版本管理、配置文件带版本号。Runner 协议与内部表结构不在承诺内 |
| N-19 | 外部可安装与可扩展 | 想法（下个大版本，U-57） | 能在干净的 Linux 上照文档安装；Harness 做成声明式配置（N-09 已用代码实现三个容器内 Harness 与共用的包装层，声明式格式在此设计）；数据集来源在 Hub 之外加本地目录与 Git（上传转换好的 Harbor task 已由 N-33 实现） |
| N-34 | 可替换的执行器 | 想法（U-88） | 现在不取代 Harbor。以后可以在 Runner 协议后面加自己的执行器，队列、记录、比较、SDK 与网页不动，主要工作在数据集与准备。启动条件，任一成立即可：Harbor task 表达不了的任务（GPU、远程环境、非容器）、每个试次一个容器的开销成为瓶颈（与 N-15 一起看）、Harbor 的方向与需求分道扬镳。见 [N-33](archive/N-33-held-out-benchmarks-workflows.md) 第 6.2 节 |
| N-38 | 脱离本机的执行机 | 想法（用户想尝试，承认超出当前需求；先暂停开发、跑一段真实实验，之后启动） | 让 Runner 在其他机器上执行。倾向拉取式的执行代理：执行机向中心领取任务，在本地调用现有 Runner，回传事件与产物；中心仍是唯一的数据来源，执行机只有缓存与临时目录，存储不分散。要解决的是任务下发与事件回传、远端拉取镜像、材料与附件的传送和核对、产物回传、模型密钥下发带来的信任边界、取消与租约及重启对账，以及镜像准备放在哪台机器。第一步是在一次性分支上做限时试验，只求一台远端执行机跑通一次 Oracle 运行并把产物传回，试验代码不合入，据此再写正式提案。与 N-34 的区别：N-34 换执行器，本项不换，只改变它运行的位置 |

### 不做或暂缓

- 自动部署（N-02）。
- [N-13 试次粒度调度](N-13-trial-granularity.md)：暂缓（U-57）。
- N-15 Docker 开销：暂缓（U-57）。原想法：每试次的容器、网络、出口控制 sidecar 开销以后会成为瓶颈；先测量各部分占比，再考虑同运行内复用容器或远程执行环境。
- N-20 确认项目名：暂时看不出必要（U-57）。原顾虑：已有多个知名项目叫 Maestro（移动端 UI 测试工具、Netflix 的工作流编排器），不构成法律冲突但影响检索。
- 多租户 SaaS（U-31）：安全边界建立在成员互相信任上，改为多租户相当于重做产品。
- 改写作者 Dockerfile 换源：只在本机代理与镜像源都无法解决时，作为显式的准备方法选项再讨论，且须计入准备规格。
- 公平调度：2～3 名活跃成员时意义不大。
- [N-21 异机备份恢复](N-21-offhost-restore.md)：暂缓。
- CLI 与 MCP（U-11）、事件流与 webhook：暂不提供。

### 被取代

| 编号 | 状态 | 提案 |
| --- | --- | --- |
| N-12 | 被 N-37 取代（U-96） | 带排空的更新与维护模式（原暂缓，U-48；无正文） |
| N-32 | 被 N-33 取代（U-85） | [留出集与任务清单](archive/N-32-held-out-evaluation.md) |
| N-31 | 被 N-33 取代（U-85） | [用 SDK 与 skill 适配工作流：以附件改进为例](archive/N-31-workflow-adaptation.md) |
| N-28 | 被 N-33 取代（U-85） | [数据集拓展与执行 / 评测拆分](archive/N-28-datasets-and-evaluation-split.md) |

### 已完成

结论已并入当前文档，细节以系统说明与代码为准。

| 编号 | 状态 | 提案 |
| --- | --- | --- |
| N-42 | 完成 | [回收站批量操作与 Harness 升级](archive/N-42-batch-recycle-harness-upgrade.md)（U-102）→ [03](../design/03-configuration-attachments.md)、[07](../design/07-artifacts-export.md)、[11](../design/11-web-ui.md) |
| N-37 | 完成 | [网页检查与触发更新，带排空](archive/N-37-web-update.md)（U-96、U-98）→ [12](../design/12-operations.md)、[05](../design/05-scheduling-execution.md)、[11](../design/11-web-ui.md)、[部署指南](../guides/deployment.md) |
| N-36 | 完成 | [工作流记录：给人读写的文字与可跳转的引用](archive/N-36-record-text.md)（U-95、U-97）→ [01](../design/01-domain-model.md)、[10](../design/10-api-sdk-cli.md)、[11](../design/11-web-ui.md) |
| N-35 | 完成 | [补齐 N-33 的网页一侧：数据集上传与版本管理、工作流记录读写、交付物导出](archive/N-35-web-dataset-upload.md)（U-92、U-93）→ [02](../design/02-datasets-preparation.md)、[08](../design/08-collections-comparison.md)、[11](../design/11-web-ui.md) |
| N-33 | 完成 | [留出集、外部 benchmark 与工作流适配](archive/N-33-held-out-benchmarks-workflows.md)（U-86～U-91）→ [02](../design/02-datasets-preparation.md)、[07](../design/07-artifacts-export.md)、[08](../design/08-collections-comparison.md)、[09](../design/09-identity-security.md)、[10](../design/10-api-sdk-cli.md)；三部分已合入，可用性检查及未覆盖范围见归档结果，评测拆分仍暂缓 |
| N-30 | 完成 | [问题记录与测试目录](archive/N-30-issues-and-test-layout.md)（U-70～U-72）→ [开发与协作](../guides/development-workflow.md)“问题与修复”、[开发验证](../guides/testing.md)“测试的组织” |
| N-29 | 完成 | [细粒度 PAT](archive/N-29-fine-grained-pats.md)（U-69）→ [09](../design/09-identity-security.md) 第 2、3 节 |
| N-26 | 完成 | [文档结构与单向引用](archive/N-26-documentation-conventions.md) |

#### 历史完成项

以下条目在归档规则生效前已删除正文，仅保留既有摘要。

- [N-27 发布流程与 CI 分工](archive/N-27-release-workflow.md)（U-66、U-67）：统一准备与续跑、权威版本、push 检查、准确 SHA 发布门禁、固定产物与开发版本收尾 → [12](../design/12-operations.md)、[发布指南](../guides/releasing.md)。
- N-25 Agent 客户端（U-59～U-61）：skill 随 SDK 发布、推荐的工作区、从 git 提交发布附件、SDK 包仓库 → [10](../design/10-api-sdk-cli.md) 第 7 节、[03](../design/03-configuration-attachments.md) 第 6 节、E2E-RUN-06。
- N-18 网页多语言（U-58）：中英文界面，英文为 AI 翻译、界面上注明 → [11](../design/11-web-ui.md) 第 1 节。
- N-24 CI 稳定（U-43、U-57）：时限、重试、可选代理、并行 pytest、只改文档时跳过；主机一侧由用户解决 → [开发验证](../guides/testing.md)。
- N-07 数据集版本操作（U-30、U-53）：检查上游更新、预览差异后添加版本、先选数据集再选版本 → [02](../design/02-datasets-preparation.md) 第 3 节、E2E-DS-01。
- N-05 产物按层级打包下载（U-30、U-53） → [07](../design/07-artifacts-export.md) 第 5 节、E2E-OUT-04。
- N-01 独立验证环境、Compose 辅助服务与本机镜像仓库（吸纳 N-14；U-54～U-56） → [02](../design/02-datasets-preparation.md)、[15](../design/15-stack-harbor.md)、E2E-DS-06。
- N-06 附件增量发布（U-50、U-51）：按内容寻址的快照、只传缺少的文件、父快照与分叉 → [03](../design/03-configuration-attachments.md) 第 6 节、E2E-RUN-05。
- N-11 集合、比较与分析快照（U-46、U-47） → [08](../design/08-collections-comparison.md)、E2E-OUT-02～03。
- N-03 网页布局（合并 N-04；U-45） → [11](../design/11-web-ui.md) 第 1、3 节。
- N-09 Codex 与 Claude Code，以及上游版本提示（U-40、U-41） → [03](../design/03-configuration-attachments.md) 第 2 节、[09](../design/09-identity-security.md) 第 5 节、E2E-RUN-07。
- N-02 持续发布（U-42、U-43、U-62）：`dev` 预发布、tag 正式发布、SDK 包仓库与 GitHub Pages 索引，不自动部署 → [12](../design/12-operations.md) 第 1 节、[打包与发布](../guides/releasing.md)。
- N-16 开源许可证 Apache 2.0：`LICENSE`、`NOTICE`、第三方许可证清单与允许列表 → [12](../design/12-operations.md) 第 1 节。
- N-08 安装网络在管理页配置（合并 N-10；U-38、U-39） → [12](../design/12-operations.md) 第 3 节、E2E-RUN-07。
- N-23 现场说明与证据进 Gitea 私有仓库（U-35、U-36）：私有仓库 `auua/maestro-site`；CI 的输出看 Actions 运行日志（U-57） → `AGENTS.md`、[开发流程](../guides/development-workflow.md)“资料与交付证据”。

## 提案模板

~~~markdown
# N-xx 标题

状态：想法 / 设计中 / 已确认 / 实施中 / 暂缓 / 完成 / 取消 / 被取代 · 优先级：一 / 二 / 三 / 暂缓 · 日期 · 相关：系统说明的链接、U-xx

## 问题

要解决什么、为什么现在要解决。

## 方案

怎么做，以及放弃了哪些做法、为什么。

## 影响

要改的系统说明、数据结构、接口、部署与验收。

## 验收

完成的标准，可以引用 13 中的场景编号。

## 待定问题

需要用户决定或实测确认的事。

## 结果

归档时说明完成、取消或被取代的结果与原因；实现后的行为链接到当前系统说明。
~~~
