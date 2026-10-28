# 15 技术栈与 Harbor 集成

导航：[总览](00-overview.md) · 相关：[执行](05-scheduling-execution.md)、[评分](06-results-scoring.md)、[网络与权限](09-identity-security.md)、[运维](12-operations.md) · 相关代码：`runner/`、`src/maestro/execution/`、`ops/maestroctl.py`、`tools/build_release.py`

## 1. 选型依据

单机、约 10 个并行试次的规模，服务端和 Worker 放在同一进程就够了。Harbor 稳定版通常两周一发，minor 版本可能含破坏性变更，所以平台与 Harbor 用独立依赖环境，通过版本化的 Runner 协议集成；子进程边界也让平台能独立演进，执行器可替换（U-15）。

## 2. 技术栈

| 层 | 采用 | 要点 |
| --- | --- | --- |
| 服务端 | Python 3.12–3.13、FastAPI、Pydantic | 业务模型独立于 Harbor 类型 |
| Harbor 集成 | `runner/` 下独立 uv 项目 `maestro-runner`，锁定 Harbor 0.24.0 | 服务进程不 import harbor，经 JSON 协议与文件交接 |
| 数据库与队列 | SQLite WAL、SQLAlchemy 2 Core、Alembic；asyncio 调度 | 同步短事务，写事务一律 `BEGIN IMMEDIATE`；按运行领取（D-020），单一协调器分配容量 |
| 产物 | 本地文件系统，`ArtifactStore` 接口 | 日志和大文件不进数据库 |
| 网页 | Vite + React + TypeScript + shadcn/ui + Tailwind | 构建产物打进服务 wheel 由服务托管，运行主机无需 Node |
| 客户端契约 | `tools/generate_clients.py` 导出 `openapi/maestro-v1.json` 并生成 SDK 模型；前端 `npm run gen:api` 从同一文件生成类型 | 生成文件入库，契约漂移是测试失败；SDK（`sdk/`，httpx）不依赖服务端或 Harbor |
| 安全 | Argon2id 密码；会话与 PAT 只存摘要；AES-GCM 凭据，主密钥在库外 | 见 [09](09-identity-security.md) |
| 发布与守护 | `tools/build_release.py` 打包，`maestroctl` 安装；systemd、journald | 依赖按带哈希的锁定清单安装，启动时不升级 |

服务依赖由根目录 `uv.lock` 锁定，Runner 依赖由 `runner/uv.lock` 锁定，二者分开。不引入 Celery、Redis、Kubernetes、独立微服务或生产 Node 服务。Alembic 不意味着 SQLite 能无成本换成 PostgreSQL：SQL、锁语义和并发测试都要重新验证。CLI 与 MCP 不提供（U-11）；`maestro` 命令只用于初始化、迁移、启动和备份等运维动作。

## 3. 两条 schema 边界

1. **平台 API**：由 FastAPI 路由和平台 Pydantic 模型导出 OpenAPI，网页类型、SDK 模型和 API 文档都从这一份生成。
2. **Harbor 作业**：只在 Runner 内出现。Runner 用锁定版本 Harbor 的 Pydantic 模型（`JobConfig`、`RetryConfig` 等）直接构造作业、解析结果，再转换为平台协议事件。

服务端不向客户端透传 Harbor 的类型、异常类或 job config，升级 Harbor 不直接改变公共 API。

已知限制：没有从锁定 Harbor 导出 JSON Schema 做适配器校验，字段路径的正确性靠 Runner 的真实 Docker 测试保证。

## 4. 进程与依赖边界

~~~mermaid
flowchart LR
    API[FastAPI 业务服务] --> W[进程内 Worker 与调度]
    W --> P[Runner 子进程 / JSON 协议]
    P --> H[独立环境中的锁定 Harbor]
    H --> D[Docker 与 Harbor 出口控制 sidecar]
    P --> F[运行工作目录 / 归档]
    W --> S[ArtifactStore]
    F --> S
    API --> B[Docker CLI：准备与清理]
~~~

Worker 在服务进程内，负责领取、租约、取消、资源和结果确认；Harbor 调用都在 Runner 子进程中。一个运行对应一个 Runner 进程和一个 Harbor Job（D-020），Job 内按运行并发执行全部试次。改为试次粒度的设想见 [N-13](../proposals/N-13-trial-granularity.md)。

系统级安装由 `maestroctl` 完成，目录见 [12](12-operations.md) 第 1 节。与 Harbor 集成相关的是服务与 Runner 的环境分开：服务环境在 `/opt/maestro/versions/<build>/venv`，Runner 环境在 `/opt/maestro/harbor/<harbor-version>/<runner-revision>/venv`。

- **Runner 修订**是 Runner wheel 与其锁定依赖清单的摘要，Harbor 锁与 Runner 代码都不变的更新复用已装环境。版本目录的 `runner-env` 链接指向它，`runner_python` 配置为 `/opt/maestro/current/runner-env/venv/bin/python`，切换 `current` 即切换 Runner，不原地升级；不再被保留版本引用的 Runner 环境随旧版本回收。开发时 Runner 在仓库 `runner/` 下。
- **发布清单** `RELEASE.json` 记录版本、提交、数据库迁移头、Harbor 版本、Runner 修订和每个文件的 SHA-256。执行器标识为 `harbor-0.24.0/<错误映射摘要>`（`src/maestro/execution/policy.py`），随运行记录。
- **Hub 目录与下载**走 Runner 的 `hub` 命令（同时运行的数量受 `hub_runner_slots` 限制）；服务进程不为读 Hub 引入 Harbor 依赖。
- **准备不经 Harbor**：服务进程直接调用 Docker CLI（`buildx build`、`pull`、`save`、`load`），按 U-17 另存镜像原件，把准备好的镜像通过任务副本的 `docker_image` 交给 Harbor。准入、缓存与共享恢复由平台管理（[02](02-datasets-preparation.md)）；准备进程是服务进程的子进程，随服务退出。
- **附件安装**在每个试次容器内由 Runner 的 Agent 包装层在 setup 阶段完成，不进入数据集的共享准备（[03](03-configuration-attachments.md)）。

已知限制：没有 `compatibility.json` 兼容清单（协议、Harbor、schema 与异常映射的摘要），一个安装只有当前版本的 Runner 在用，不支持多个 Harbor 版本并存调度。

## 5. Runner 协议

平台自定义的协议，Harbor 不直接输出这些事件。实现见 `runner/src/maestro_runner/protocol.py` 与 `src/maestro/execution/harbor.py`。

| 通道 | 内容 | 要求 |
| --- | --- | --- |
| 启动 | `python -I -m maestro_runner`，新会话 / 进程组 | 不经 shell；环境只继承白名单变量，不含提供商密钥 |
| stdin | 一个 JSON：protocol_version、命令（`execute` / `hub` / `probe`）、运行规格 | `execute` 的规格含 run_id、任务路径与 trial_ids、Agent、网络计划、标签、重试设置、产物上限、凭据 |
| stdout | NDJSON 事件，一行一个 | envelope 为 protocol_version、run_id、无间断的 sequence、type、at、payload；Runner 在导入 Harbor 前把 fd 1 留给协议，其余输出全部转到 stderr |
| stderr | 诊断输出 | 服务持续读取，只保留有界的头尾并脱敏，防止管道阻塞；不作评分依据 |
| 工作目录 | 每个执行尝试的归档目录 | Runner 按产物上限复制，超限的列为跳过；服务入库时对文本文件脱敏 |

`execute` 的事件：started（Harbor 与 Runner 版本、试次数、PID）、network_plan、attempt_started、phase_changed、log、attempt_finished（结果、是否还会重试、归档目录与逐文件摘要清单、用量、耗时、附件安装记录）、heartbeat、cancel_requested、completed。`hub` 与 `probe` 只回一条 result。阶段事件来自 Harbor Job 的试次生命周期钩子（`Job.add_hook`），不解析终端输出；log 事件是 Runner 定期读取 Agent 与验证日志、渲染 Agent 轨迹得到的。

没有单独的结果目录完成标记：平台按 attempt_finished 的清单逐个核对归档文件的摘要后入库。进程退出码（0 完成、2 Runner 错误、3 取消、4 拒绝）不决定业务结果，平台按事件、租约和错误策略决定试次状态；退出码 0 不等于任务通过，非零也不抹去已确认的结果。Runner 长时间无事件（`runner_silence_sec`）视为失联并停止。

**凭据。** 服务在执行时解密凭据，经 stdin 交给 Runner，不放进命令行、环境变量或可导出的作业配置。模型密钥只留在 Runner 内存；Terminus-2 在 Runner 内调用模型，OpenCode、Codex、Claude Code 在容器内调用，密钥以权限 400 的临时文件进入容器，Agent 结束即删除（[09](09-identity-security.md) 第 5 节）。作者参数值（含秘密参数）放进 Runner 环境变量，供 Harbor 解析任务模板。stderr 中出现的密钥值与带口令的代理地址由服务端脱敏。

## 6. 作业生成与预检

一个运行生成一个 Harbor Job（D-020，`runner/src/maestro_runner/job.py`）：

- 任务列表为该运行的全部任务（每个任务一份私有副本，写入准备好的镜像与收紧后的网络策略）；`n_attempts` 为每任务计划试次数，所以一个运行内各任务的试次数必须相同；`n_concurrent_trials` 为运行并发。
- `RetryConfig` 由平台错误策略推出并显式下发：最大重试次数、退避参数，以及不重试的异常名单（`exclude_exceptions`）；不使用 Harbor 默认名单，平台也不在外层再重试，避免两层相乘（U-15、D-006）。Harbor 内的每次重试在平台记为一次执行尝试。
- 资源与时限覆盖（CPU、内存、Agent / 验证超时）、平台标签的 compose override 一并写入。

Agent 必须显式指定：Harbor 的默认 Agent 是 oracle，不能因为漏填而执行参考答案。Oracle 只作为 `execution_mode = oracle` 的测试模式，不混入模型能力比较。

平台预检在受理时完成：内容固定、任务准入、已知内容阻塞、必要配置、资源、权限，以及模型运行所需的网络收紧能力（启动时用 Runner 的 `probe` 命令探测）。本机缓存缺失只形成运行恢复计划，不要求管理员重新准入。

已知限制：Harbor 的 `--dry-run` 与 `--print-config` 没有接入预检。`--dry-run` 本来也只检查配置、Agent 参数、凭据和任务来源，不能证明镜像可构建或网络可用。

## 7. 取消、资源与服务重启

Runner 在独立会话与进程组中启动。取消先发 SIGTERM，Runner 协作停止（Harbor 结束各试次并停止容器），宽限期（`runner_term_grace_sec`）后向进程组发 SIGKILL。服务进程退出时 Runner 通过 `PR_SET_PDEATHSIG` 收到 SIGTERM；协议管道断开时也按取消处理，不继续执行。

结束进程组不能保证 Docker daemon 管理的容器、网络和出口控制 sidecar 都已消失。所以每次执行后都按标签清理：运行的容器、网络和构建出的镜像都带 `io.maestro.instance_id` 与 `io.maestro.run_id`，清理只删带这些标签的资源，再确认没有残留（`src/maestro/execution/docker.py`）；其他实例、无标签和手工创建的资源不碰。清理失败保留记录，运行的资源预留在确认释放前不归还。

服务意外退出可能留下 Runner 与容器。启动对账检查持久记录中的进程身份：PID 之外还比对内核启动时刻与 boot id，只向确属自己启动的进程组发信号，再按标签清理；保留已确认评分，未完成部分按 [05](05-scheduling-execution.md) 标记中断，不自动续跑。准备的租约与构建资源同样在启动时对账，不因能够重建缓存而续跑旧试次。

一个等待运行取消不终止共享的准备进程，最后一个等待者退出才取消。准备构建带 `io.maestro.spec_digest` 标签，超时单独设置（`src/maestro/config.py`）。

已知限制：镜像构建只受同时恢复数 `restore_slots` 限制，CPU / 内存不计入主机容量。

## 8. 复用 Harbor 的语义与升级风险

| 能力 | 结论 | 平台处理 |
| --- | --- | --- |
| 错误分类 | `error_policy.py` 只在 Harbor main 与开发构建中，0.24.0 没有 | 平台自定策略（U-15），Harbor 公开分类只作初值参考；适配器把锁定版本的异常类型名映射到 [06](06-results-scoring.md) 的规则，映射摘要进执行器标识 |
| 分阶段网络 | Harbor 的 Docker 环境支持 allowlist 与阶段切换，依赖 nftables；作业级 `extra_allowed_hosts` 只能加主机 | Runner 改写任务副本收紧网络（作者与平台取更严、白名单取交集），不自建出口代理（[09](09-identity-security.md)）。不支持时拒绝运行，不退回 public |
| Hub 引用 | `datasets[].ref` / `tasks[].ref` 支持标签、修订或 digest | 标签解析后固定 digest，上游摘要与本地材料摘要分开 |
| Oracle | 执行任务自带的参考解法，solution 目录可选 | 选有参考解法、无需外部模型的任务作真实链路测试 |

Runner 依赖 Harbor 的私有接口与内部行为，升级 Harbor 时须逐项复核：

- 包装 `DockerEnvironment._apply_network_policy`：受限后不再回到 public，并拒绝未经代理重定向的 TCP（M1 发现 Harbor 会在阶段之间恢复 public、旧连接不受新策略约束）。
- 调用 `DockerEnvironment._egress_control_kernel_support` 做网络能力预检；它在没有 `/proc/config.gz` 时乐观放行，所以真实收紧另有测试覆盖。
- Terminus-2 为在 Runner 内传入密钥覆盖了 `_init_llm`。
- 容器内 Harness 的包装层覆盖了 `_env_sources`（不读 Runner 环境）与 `install`（已有同一版本则跳过）；Codex 覆盖 `_resolve_auth_json_path` 并依赖 `_REMOTE_CODEX_SECRETS_DIR`，Claude Code 依赖 `config` 作为 `--settings` 上传与 `ANTHROPIC_BASE_URL` 的读取方式，并重写了安装命令。实时日志按 `agent/opencode.txt`、`agent/codex.txt`、`agent/claude-code.txt` 的事件格式渲染（`job.py`），上游改格式时只影响可读日志，不影响评分。
- Runner 照搬 Harbor 0.24.0 TrialQueue 的重试规则推断 attempt_finished 中的“是否还会重试”（`_will_retry`）。

## 9. 为什么仍然自建

Hosted Harbor 已提供通过网页、CLI、API 发起远程作业的能力（需申请开通），所以“Harbor 没有网页或 API”不是自建理由。Maestro 要的是：

- 自托管与本地 Docker，执行资源、产物和运行策略由团队掌握；
- 团队自己的模型凭据、Harness 与不可变扩展包；
- Agent 作为操作者，有 PAT、能力发现、预检、幂等提交和可接续的操作记录；
- 统一保存配置来源、重跑关系与本地保留规则，便于按快照复现（也是 U-31 开源与公开结果两个方向的基础）。

这不是断言 Hosted Harbor 永远不提供同类能力。不接入 Hosted Harbor 作为远程执行后端；若它将来能满足部署、权限、扩展与保留要求，应重新比较自建的维护成本。

## 10. 技术验证编号

TECH-01～04 是重写前对 Harbor 集成风险的四项验证，已在 M1 用原型完成，结论与当时的缺口见[M1 验证记录](records/m1-validation.md)（对应 V-10、V-07、V-05、V-11）。代码注释仍引用这些编号：

| 编号 | 验证的问题 | 落在代码中的做法 |
| --- | --- | --- |
| TECH-01 | 子进程能否实时、有序地报告阶段，协议不被终端输出污染，异常时有最终状态 | Job 生命周期钩子产生阶段事件；Runner 导入 Harbor 前独占 stdout（第 5 节） |
| TECH-02 | 能否按阶段收紧任务网络，不放宽作者更严的约束，缺能力时拒绝 | 改写任务副本 + 包装 Harbor 策略切换（`runner/src/maestro_runner/network.py`） |
| TECH-03 | 不运行 Agent 能否独立准备、按原 digest 恢复并共享等待 | 服务直接调用 Docker 做可复现构建与镜像原件（`src/maestro/execution/preparation.py`、`materials.py`） |
| TECH-04 | 成员上传的附件能否在每个试次容器内独立安装，失败只影响本试次 | Agent 包装层在 setup 中复制、校验并限时运行 `bootstrap.sh`（`runner/src/maestro_runner/agents.py`） |

## 11. 未满足的接入项

以下集成项还没有做到，升级 Harbor 前宜先补上：

1. 未满足：没有从锁定 Runner 导出 Harbor 作业 schema 用于适配器校验（第 3 节）。
2. 未满足：Harbor `--dry-run` 未接入预检（第 6 节）。
3. 未满足：`compatibility.json` 兼容清单未实现，异常映射的版本与摘要只体现在执行器标识中（第 4 节）。
4. 未满足：镜像构建资源不计入主机容量（第 7 节）。
5. 依赖 Harbor 私有接口与内部行为（第 8 节），每次升级须人工复核。

其余接入项（服务端不加载 Harbor、Job 内重试显式下发、Oracle 真实链路、错误映射、分阶段网络、标签清理、客户端生成、共享恢复、附件逐试次安装）已由测试覆盖，见 [13](13-testing-plan.md)。

## 12. 核查来源

以下 Harbor 官方资料于 2026-10-22 读取核对；网站内容会更新，升级时针对锁定版本重新核对：

- [文档索引](https://docs.harborframework.com/llms.txt)、[Release policy](https://docs.harborframework.com/contributing/release-policy.md)、[Job configs](https://docs.harborframework.com/jobs/configs.md)、[Error classification](https://docs.harborframework.com/jobs/error-classification.md)、[错误策略源码（main）](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/trial/error_policy.py)
- [Network policies](https://docs.harborframework.com/tasks/network-policies.md)、[Datasets](https://docs.harborframework.com/datasets/datasets.md)、[Task solution](https://docs.harborframework.com/tasks/solution.md)、[Hosted Harbor](https://docs.harborframework.com/hosted-harbor/index.md)
