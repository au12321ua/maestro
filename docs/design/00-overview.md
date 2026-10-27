# 00 总览

导航：[系统说明入口](README.md) · [决策记录](14-decisions.md) · [提案](../proposals/README.md)

## 1. 定位与使用闭环

Maestro 是自托管的 Agent 评测平台：在本机 Docker 中用 Harbor 执行“模型 × Harness × 数据集”的实验，固定配置与内容，保留评分与证据，供人和 Agent 通过网页与 SDK 提交、观察、比较和重跑。

现在服务一个受邀的可信团队：成员只用浏览器或 SDK，不登录服务器，管理员的日常配置也在网页完成。按用户 2026-10-27 的决定（U-31），在内部使用之外同时尝试两个方向：作为**开源的自托管平台**供其他团队安装，以及用平台跑标准化评测并**公开评测结果**；相关工作见[提案索引](../proposals/README.md) N-16～N-20。不做多租户 SaaS：现有安全边界建立在成员互相信任上（第 3 节），改成多租户相当于重做产品。

使用闭环：

1. 管理员在网页添加 Harbor Hub 上明确版本的数据集；任务首次准备成功后准入。
2. 成员或 experiment PAT 查询可提交目录，解析配置并查看与作者基准的差异；缓存缺失只提示运行时恢复。
3. 服务端在一个事务内受理运行，产生不可变快照、试次计划和操作记录。
4. Worker 在本机 Docker 中执行，分别记录执行、评分、产物与清理情况。
5. 网页与 SDK 查询结果、下载或批量导出；按原配置重跑，或修改后创建新运行。

为什么不直接用 Hosted Harbor 见 [15](15-stack-harbor.md) 第 9 节。

## 2. 基本约束与技术路线

| 约束 | 含义 |
| --- | --- |
| 一个实例一个团队，只有管理员和成员 | 业务结果团队可读，写操作按创建者与角色控制 |
| Agent 只参与实验，不管理平台 | PAT 即使由管理员创建也不获得管理能力 |
| Linux 单机、单服务实例、本机 Docker | 没有多机调度、GPU 或其他环境后端 |
| 重跑和修改后运行都创建新运行 | 原运行不追加试次，不被后续配置覆盖 |
| 必须有总运行时限与无人值守的提交上限 | 受理时检查时限与 PAT 额度，执行时按时限停止 |
| 版本准入与准备缓存分开 | 缓存回收不撤销准入；运行自动恢复同一内容，并发等待共享一次恢复 |
| 附件安装属于已授权运行 | 成员 / experiment PAT 可触发；只在本试次容器内，除本试次输出目录外不挂宿主目录（U-16），受超时和阶段网络限制 |
| 从空实例初始化 | 不迁移旧系统数据 |

技术路线：FastAPI 模块化单体，Worker 在服务进程内；Harbor 在独立 uv 环境中由 Runner 子进程调用，一个运行对应一个 Harbor Job（D-020）；SQLite WAL 保存业务数据与持久队列；公共 OpenAPI 同时生成网页与 Python SDK 的类型；`maestroctl` 把发布包装为 systemd 服务。选型与原因见 [15](15-stack-harbor.md)。

## 3. 提供什么、不提供什么

| 模块 | 提供 | 不提供 |
| --- | --- | --- |
| 数据集（[02](02-datasets-preparation.md)） | Hub 搜索与导入、不可变版本、管理员首次准备与准入、局部可提交、运行内共享恢复 | Hub 之外的来源（N-19）；独立验证环境与辅助服务（N-01）；跨任务共享镜像（N-14） |
| 配置（[03](03-configuration-attachments.md)） | 团队模型配置与凭据、Harness（Terminus-2、OpenCode）与 Oracle 测试模式、附件、分层解析与内容快照 | 通用容器内 Harness 如 Codex、Claude Code（N-09）；个人模型 key |
| 提交（[04](04-plans-submission.md)） | 方案、单次与批次提交、幂等、PAT 额度、重跑与修改后运行、取消 | 实验矩阵生成；private 实验 |
| 执行（[05](05-scheduling-execution.md)） | 按受理顺序领取（大运行不挡放得下的小运行）、资源准入、重启对账、环境清理 | 试次粒度调度（[N-13](../proposals/N-13-trial-granularity.md)）；公平调度；多机 Worker；GPU；金额或 token 预算 |
| 结果（[06](06-results-scoring.md)、[07](07-artifacts-export.md)） | 运行与任务结果、基本统计、日志、下载、批量导出、回收站 | 集合、对齐比较与分析快照（[N-11](../proposals/N-11-collections-comparison.md)） |
| 访问（[09](09-identity-security.md)、[10](10-api-sdk-cli.md)、[11](11-web-ui.md)） | 网页、PAT 与 Python SDK、能力发现、统一操作记录 | 实验用 CLI 与 MCP（U-11）；事件流与 webhook；开放注册与 SSO |
| 运维（[12](12-operations.md)） | 安装、启停、状态、日志、同机备份恢复、更新、保留数据卸载 | 带排空的更新（[N-12](../proposals/N-12-drain-update.md)）；异机恢复（[N-21](../proposals/N-21-offhost-restore.md)） |

费用与 token 只记观测值，未知为空；运行时长和并发不是金额预算保证。安全边界假设成员互相信任：Docker 组约等于 root，附件可运行任意 `bootstrap.sh`，证据只做基本脱敏（U-10）。

## 4. 规模与容量

按约 15 人、活跃 2～3 人的团队设计：同时执行约 10 个试次，单试次通常 1 核、2 GB、产物不超过 100 MB，不需要独立的队列集群。并发受运行并发、主机槽位与 CPU / 内存、提供商并发共同约束（[05](05-scheduling-execution.md) 第 3 节）；上限在团队策略（管理员网页）与 `src/maestro/config.py` 中。一周 1000 个试次的产物约 100 GB，镜像原件、准备缓存和导出包另计；主机还须为系统、数据库、出口控制 sidecar 和镜像构建留余量。

已知限制：10 试次并行压测未做，列表与详情请求 p95 小于 1 秒只是目标；镜像构建的 CPU / 内存不计入主机容量（[02](02-datasets-preparation.md)）。

## 5. 架构与依赖方向

~~~mermaid
flowchart TB
    Web[网页] --> API[统一 API 与身份权限]
    SDK[Python SDK] --> API
    API --> Catalog[数据集与配置目录]
    API --> Runs[方案、提交与运行]
    API --> Results[结果、产物与导出]
    Runs --> Queue[持久队列与调度]
    Queue --> Worker[进程内 Worker]
    Worker --> Executor[HarborExecutor]
    Executor --> Runner[Runner 子进程 / 锁定 Harbor]
    Runner --> Docker[本机 Docker]
    Catalog --> Source[HubSource，经 Runner]
    Catalog --> Env[DockerEnvironment 准备]
    Env --> Docker
    Worker --> Store[ArtifactStore / 本地文件]
    Runs --> DB[(SQLite WAL)]
    Results --> DB
    Catalog --> DB
~~~

业务层使用自己的 DatasetVersion、TaskAdmission、RunSnapshot、TrialResult 等对象；Hub 响应在数据源适配器中转换，Harbor 的配置与结果类型只出现在 Runner 内，服务进程不 import harbor。外部边界是 `src/maestro/boundaries/` 中的四个协议：DatasetSource（`HubSource`）、Executor（`HarborExecutor`，含按标签清理与重启对账）、Environment（`DockerEnvironment`，服务进程直接调用 Docker 准备与恢复镜像）、ArtifactStore（`LocalArtifactStore`）；测试替身在 `src/maestro/testing/fakes.py`。管理员首次准备与运行内恢复调用同一准备接口，权限入口与准入处理分开。

## 6. 跨模块不变量

1. 配置、试次身份和来源关系受理后固定；执行状态、结果与产物索引按生命周期更新。
2. 一个计划试次最多有一份被接受的最终结果；旧 Worker 的迟到上报不能覆盖新的执行。
3. 验证器给出且 reward_valid 为真的 0 是有效分数；没有评分保持空值；无效的诊断奖励不纳入统计。
4. 受理运行、消耗 PAT 额度、写入试次计划和幂等结果属于同一事务。
5. 已有运行不因修改方案、目录条目、默认值而被重写；安全撤销和实时容量仍影响其可执行性。
6. 清理失败不推翻已有评分；产物过期不删除运行、评分和证据的过期标记。
7. 网页与 SDK 共用业务服务；PAT 的能力限制在服务端强制执行。
8. 重启恢复对管理状态的掌握，不自动续跑上一次容器中的执行。
9. 准入、固定内容可得性、本机缓存分别记录；回收与暂态恢复失败不撤销准入，恢复不得换成其他 digest。
10. 共享恢复有独立租约与等待关系，单个等待运行取消不终止其他运行所需的工作；附件安装在每个试次容器内独立进行。

## 7. 文档如何组织

系统说明（00～13、15）只描述 `dev` 上现在的系统；决定在 [14](14-decisions.md)，未实现的设计在[提案](../proposals/README.md)，重写期间的实施记录冻结在 [records](records/README.md)。阅读顺序见[系统说明入口](README.md)，测试与验收场景见 [13](13-testing-plan.md)。
