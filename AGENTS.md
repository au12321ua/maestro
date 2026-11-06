# 项目工作入口

Documentation level: L3
Integration branch: dev
Release branch: master

Maestro 的文档导航在 [docs/README.md](docs/README.md)，架构在 [docs/architecture](docs/architecture/README.md)，系统行为与取舍在 [docs/design](docs/design/README.md)，待实现的设计在[提案](docs/proposals/README.md)。先读[系统说明入口](docs/design/README.md)和[开发交接](docs/HANDOFF.md)，开发前再读[分支与协作规则](docs/guides/development-workflow.md)，然后按当前任务读取相关模块和原始文件；启动时不必读完整个文档目录。

小任务（可回滚，不改接口、数据结构、密钥、部署和验收标准，例如查问题、清理环境、改文档或配置）只读与当前任务直接相关的文件。但操作服务、state 或镜像前，仍须先读部署现场说明（私有仓库 `auua/maestro-site` 的 `hosts/` 中对应主机的文件）；“工作原则”中保护现场与秘密的要求同样适用。现场说明由部署者维护，不进入公开仓库。

单项工作从 `dev` 新建短期分支和独立 worktree，不在部署目录或 `master` 上开发；只改文档时，除非用户明确要求，只新建分支、不建 worktree（U-52）。开始工作时核对相关提案和本次明确任务，用户当前明确指令优先；[决策记录](docs/design/14-decisions.md)是出处账本，追溯某条规则的来由时再查，平时不必通读（U-94）。

## 需求、设计与文档的层次

1. 用户当前明确指令，以及已登记的用户决定。用户决定登记在[决策记录](docs/design/14-decisions.md)第 0 节，同时落进它约束的地方（本文件、系统说明或提案），按那里的现状执行。
2. 已确认的提案：`docs/proposals/` 中状态为“已确认”或“实施中”的提案是实施依据；“想法”“设计中”的提案不是授权，按它实现前先与用户确认范围和待定问题。
3. 系统说明：`docs/design/00`～`13`、`15` 描述系统现状。实践中发现更好的做法或说明有误时优先实践，同一提交里改写系统说明；涉及用户边界或重要取舍的，先在决策记录登记。

需求审计稿（`docs/design/requirements-review.md`）与重写期间的实施记录（`docs/design/records/`）已冻结，只作出处，不再修改或新增（U-33）。决策记录“冲突与结论”中尚未定论的条目，未经用户确认前不按任何一方擅自实现。

增量的流程：写提案 → 用户确认 → 实现 → 在合入的同一个 PR 中把结论并入系统说明、把提案标记完成并移入 `docs/proposals/archive/`、更新提案索引 → 实施证据写在提交说明与 PR 描述中。取消或被取代的提案也归档并注明原因与替代项；暂缓的保留在提案目录并标明状态。既有 N 编号不复用，已删除的旧提案不恢复（U-64）。系统说明只描述现状，不追加“原设计 / 现状 / 阶段”式的分层注解，代码能表达的细节（字段、接口清单、默认值）以代码为准。

只有用户明确决定才登记为 U 条目；新条目附用户原话或可追溯对话出处，旧条目不追补来源。登记的同一提交里把决定落进它约束的地方。旧条目的正文与编号保留；替代决定追加新条目，只在新条目里注明取代谁，不回改旧条目。账本只在用户要求时整理（U-94）。Agent 的实现选择写入技术决定或提交说明，不能登记为用户约束。

文档可以引用代码路径、符号与测试；代码注释和测试说明独立写清行为、原因与不变量，不引用文档路径、章节或内部决定、提案、验收编号。U、N 与验收场景号保留在文档、提交说明和 PR 中，文档章节可按内容重排；不新增引用检查器或引用注册表（U-65）。代码注释可以附 issue 引用（`Gitea#N`），但先写出约束本身（U-71）；问题记录与修复收尾见[开发与协作](docs/guides/development-workflow.md)“问题与修复”（U-70）。

## 目标与用户确认过的边界

项目改名为 Maestro：让受邀的可信团队通过网页和 SDK 提交、观察、比较和重跑实验；数据集可选择，实验配置固定，结果全团队共享。成员只用浏览器、不登录服务器完成实验操作，管理员日常配置也在网页完成。在服务团队之外，产品方向是同时尝试开源的自托管平台与公开评测结果，不做多租户 SaaS（U-31）。

下列边界已经用户确认，除非决策记录中有更新的用户决定：

- 保留个人提交身份，仅成员和管理员两种角色；管理员邀请账户，不开放注册，不引入复杂 SSO。
- 成员取消自己的任务；管理员可取消任何任务、管理团队凭据与容量；重跑归当前提交者。
- 除主密钥外都可删除，先进回收站，再在回收站中物理删除；被删数据集关联的运行记录保留。
- 团队共用多提供商、多 key；明确绑定、加密存储，主密钥在数据库之外；密钥不回传前端、不进入日志。
- 网页与 SDK 共用队列、记录和容量；保留幂等、持久排队、取消与资源限制，区分有效零分、缺评分和执行失败。暂不提供 CLI 与 MCP（U-11）。
- 证据从宽：日志与产物经基本脱敏后即可查看，不设审计门。只有执行资源未确认释放才占用清理阻塞容量。
- 前端为 Vite + React + shadcn/ui + Tailwind；同一可配置 HTTP 服务提供构建页面与相对 API；HTTPS、代理、证书和域名由部署环境处理。部署采用设计中的系统级布局。
- 首版没有个人模型 key 模式、private 实验或复杂权限体系；运行时长和并发不是金额预算保证。不迁移旧数据；在第一个面向外部用户的版本之前不承诺向后兼容（U-31）。

## 按任务查阅

- 下一步做什么：[提案索引](docs/proposals/README.md)。测试分层与验收场景：[测试与验收](docs/design/13-testing-plan.md)。各模块从[系统说明入口](docs/design/README.md)进入。
- 全局结构：[系统架构](docs/architecture/README.md)；代码分工：[源码职责](docs/architecture/source-layout.md)；公开 API 以服务导出的 OpenAPI 为准。已删除的基线服务、CLI 与机器 API 可查 `9ff43b5`。
- 验证命令与隔离前提：[开发验证](docs/guides/testing.md)；版本与发包：[打包与发布](docs/guides/releasing.md)；安装维护：[部署指南](docs/guides/deployment.md)。
- 部署现场以私有仓库 `auua/maestro-site`（Gitea；克隆与上手步骤见其 README）`hosts/` 中的现场说明为准；操作前实时核对身份与引用。
- 废弃方案和重写前的旧实现可查 Git 历史，不成为新增约束。

## 运行与验证入口

当前实现需要 Linux、Python 3.12–3.13、uv；实际执行另需 Docker/Compose、可用镜像及 Hub 数据集权限。依赖由 `pyproject.toml`、`uv.lock`、`frontend/package-lock.json` 记录。初始化运行 `uv sync --locked --group dev`；网页在 `frontend/` 执行 `npm ci`。Python 的检查入口为 `uv run ruff check src tests runner/src tools sdk/src ops` 与 `uv run pytest -q`；文档运行 `python3 tools/check_doc_links.py` 与 `git diff --check`。完整检查、开关与隔离前提以[开发验证](docs/guides/testing.md)为准。

任务 worktree 通常从 `origin/dev` 建立：`git fetch origin` 后执行 `git worktree add -b feature/<topic> ../harbor-helper-<topic> origin/dev`；任务目录可按当前环境选择。只改文档时在干净且由自己使用的检出上执行 `git switch -c docs/<topic> origin/dev`。合入 `dev` 后，核对任务树的未提交、未跟踪和 ignored 内容及服务引用，再精确执行 `git worktree remove <任务目录>` 和 `git branch -d <任务分支>`；无法证明安全时保留并说明原因。推送、发布、合入 `master`、打 tag 和删除远端分支在用户授权范围内执行。

## 工作原则

区分本轮观察、版本限定的历史证据、文档声称及推测；未经用户确认的 Agent 方案、默认值或实现选择不能升格为硬约束。变更按影响范围验证，并记录代码版本、前置条件及未覆盖范围。

现场报告、原始日志、capture、截图和证据索引放私有仓库 `auua/maestro-site`，CI 的输出看 Gitea Actions 的运行日志（U-57）；ignored `.local/` 只放本机草稿。`maestro-site` 不放主密钥文件、初始化令牌、Gitea 凭据和 SSH 私钥，其余日志不要求逐项脱敏（U-36）。公开文档只留短的固定版本范围摘要；不要求将 Agent 提供的原始证据重新入库。历史笔记和报告按原时点保留，不随任务反复同步为“最新状态”。集成后负责人必须按协作规则收尾 worktree 和短期分支。

保护已有现场与秘密：不把凭据或运行数据入库，不用已有团队 state 或真实凭据作随手测试；真实模型请求须核对当前任务授权与预算。开发与测试中的实验只用全新的独立 state。
