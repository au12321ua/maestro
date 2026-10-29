# 源码职责

服务在 `src/maestro/`，Runner 在 `runner/`（单独的 uv 项目），Python SDK 在 `sdk/`（单独的包 `maestro_sdk`），网页源码在 `frontend/`，构建到 `src/maestro/static/`。本页描述当前代码的分工；系统行为见[系统说明](../design/README.md)，公开 API 以 `openapi/maestro-v1.json` 为准。

## 服务（`src/maestro/`）

| 板块 | 位置 | 职责 |
| --- | --- | --- |
| 入口 | `cli.py`、`config.py`、`application.py` | `maestro init / db upgrade / serve / backup / restore / activity / openapi`；读取并校验 `config.toml`；组装数据库、边界与服务 |
| 备份 | `backup.py` | 同机备份集（SQLite 一致快照、配置副本、可选业务文件、清单）；停服恢复（核对清单、结构版本与所需主密钥）；更新前查询在途工作（`activity`） |
| 基础 | `core/` | 不透明 ID、UTC 时间文本、规范 JSON 与 `sha256:` 摘要、统一错误、请求上下文、单实例锁 |
| 数据库 | `db/` | 逻辑表（`schema.py`）；SQLite WAL / 外键 / busy_timeout，写事务一律 `BEGIN IMMEDIATE` 短事务（`engine.py`）；Alembic 迁移（`migrate.py`、`migrations/`） |
| 领域 | `domain/` | 运行 / 试次 / 尝试 / Operation 的状态与独立维度、聚合规则（`states.py`）；评分契约与运行汇总（`scoring.py`）；跨运行比较的维度、失败分组、对齐与统计（`comparison.py`）；团队策略内置初值与结构性常量（`limits.py`）；安装网络的内容与校验（`setup_network.py`） |
| 安全 | `security/` | Argon2id 密码、带前缀的随机令牌与摘要、主密钥代次与轮换、AES-GCM、基本脱敏 |
| 业务服务 | `services/` | 账户、初始化、邀请、会话与认证（`accounts.py`），PAT，凭据，权限矩阵（`principal.py`），审计与管理员查询（`audit.py`、`audit_queries.py`），幂等，Operation，分页游标，实例标识；目录读取（`catalog.py`），数据源搜索与数据集管理（`datasets.py`），导入、首次准备、共享缓存恢复、回收与重启对账（`preparation.py`）；团队策略修订（`policy.py`），安装网络版本（`setup_network.py`），模型配置与 Harness 版本（`model_configs.py`），Harness 上游版本查询（`upstream.py`），附件快照与增量发布（`attachments.py`），实验方案（`plans.py`），配置解析（`configuration.py`）；运行受理、查询、取消、重跑、修改后运行与提交批次（`runs.py`），运行级 Worker（`worker.py`），执行容量与提供商并发（`capacity.py`），运行汇总（`results.py`），比较、集合与分析快照（`collections.py`）；产物索引、分段读取、到期回收与对账（`artifacts.py`），批量导出（`exports.py`），两步删除与回收站（`recycle.py`、`deleted.py`），磁盘水位（`disk.py`），导出构建与回收的维护循环（`maintenance.py`） |
| 边界 | `boundaries/` | 数据集来源、实验执行器、运行环境、文件存储四个接口；业务层只见这些平台类型 |
| 执行 | `execution/` | Harbor 执行器：启动 Runner 子进程、解析协议、上传产物（`harbor.py`）；平台错误策略与 Harbor 异常映射（`policy.py`）；Runner 进程身份与进程组终止（`process.py`）；按标签清理 Docker（`docker.py`）；Hub 数据源，经 Runner 调用（`hub.py`）；Docker 准备环境：可复现构建或固定拉取、导出原件、载入或重建并核对（`preparation.py`）；模型协议、Harness 参数与秘密检查（`agents.py`）；附件快照的清单检查、按 SHA-256 存放内容与还原目录（`attachments.py`）；任务材料缓存、任务检查与作者参数发现、准备规格、开发用本地任务来源（`materials.py`） |
| 测试替身 | `testing/fakes.py` | 四个边界的假实现，供测试和 `serve --demo-executor` 演示 |
| HTTP | `api/` | FastAPI 应用、`{data, meta}` 外壳与结构化错误、静态网页（`app.py`）；会话 cookie + CSRF 与 PAT Bearer 认证（`deps.py`）；公开模型（`models.py`）；按资源分的路由（`routes/`，处理函数名即 OpenAPI 的 operationId）；不依赖部署的 OpenAPI 导出（`openapi.py`） |

服务进程从不导入 Harbor（`tests/maestro/test_harbor_boundary.py` 检查）。

## Runner（`runner/`）

单独的 uv 项目，锁定 Harbor 版本；由服务以 `python -I -m maestro_runner` 启动，经 stdin 收输入、stdout 的 JSON 行协议上报。`protocol.py` 在导入 Harbor 前接管 stdout；`job.py` 把一个运行作为一个 Harbor Job 执行，上报步骤、日志与每次尝试的结果；`agents.py` 是模型 Harness 适配器与附件安装；`network.py` 做平台网络收紧；`hub.py` 是 Hub 目录命令（搜索、版本、解析、按 digest 下载并核对）。

## 契约、客户端与网页

| 板块 | 位置 | 职责 |
| --- | --- | --- |
| 公开契约 | `openapi/maestro-v1.json` | 由服务导出、受版本控制的 OpenAPI；网页类型与 SDK 模型都从它生成，服务在 `/capabilities` 报告它的摘要 |
| 生成 | `tools/generate_clients.py` | 导出契约并生成 SDK 的 `models.py` 与 `_contract.py`；`--check` 只比较。网页类型在 `frontend/` 用 `npm run gen:api` / `check:api` |
| Python SDK | `sdk/src/maestro_sdk/` | 只依赖 httpx 与 pydantic：生成的模型，以及手写的认证、幂等提交与重试、翻页、等待、核对摘要的下载（`client.py`）、错误类型（`errors.py`）。用法见 `sdk/README.md` |
| Agent skill | `skills/maestro-sdk/SKILL.md` | 让 Agent 经 SDK 操作 Maestro 的流程与报告规则；提到的 SDK 调用由 `test_sdk.py` 核对 |
| 网页 | `frontend/src/` | Vite + React + shadcn/ui：页面（`pages/`，管理页的配置标签在 `pages/admin/`）、API 调用与生成的类型（`lib/`）、公用组件（`components/`）；Playwright 冒烟在 `frontend/qa/` |

## 安装、发布与检查

| 板块 | 位置 | 职责 |
| --- | --- | --- |
| 安装管理 | `ops/maestroctl.py` | 只用系统 Python 标准库的 `maestroctl`：检查、安装、启停、状态、日志、备份恢复、更新与失败回退、卸载，按 [12](../design/12-operations.md) 的系统级布局；发布包随附一份。用法见[部署指南](../guides/deployment.md) |
| 打包与发布 | `tools/build_release.py`、`tools/third_party_licenses.py`、`tools/publish_release.py` | 打出含服务 / Runner / SDK wheel、带哈希锁定的依赖、`INSTALL.md`（即部署指南）、`LICENSE`、`NOTICE`、第三方许可证清单与 `RELEASE.json` 的压缩包；许可证清单只允许列表内的许可证；CI 把产物发布到 Gitea Releases |
| 测试 | `tests/maestro/` | pytest；分层与开关见 [13](../design/13-testing-plan.md) 第 2 节 |
| CI 与检查 | `.gitea/workflows/verify.yml`、`.github/workflows/release.yml`、`tools/check_doc_links.py` | Gitea：每次推送运行 lint、Markdown 相对链接、不含 Docker / Hub / 模型的 pytest（含契约与 SDK 模型漂移）、网页构建与许可证检查，`dev` 与版本 tag 通过后发布；GitHub：只在推送版本 tag 时构建并发布。命令见[开发交接](../HANDOFF.md)的验证入口 |
