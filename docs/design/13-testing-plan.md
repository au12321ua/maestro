# 13 测试与验收

导航：[总览](00-overview.md) · 依赖：全部模块 · 相关代码：`tests/maestro/`、`frontend/qa/`、`.gitea/workflows/verify.yml`；命令见[开发交接](../HANDOFF.md)的验证入口

## 1. 使用方式

本文列出验收场景（E2E-…、F-…）与需求编号的验收入口，并注明当前由哪些测试覆盖；没有自动化测试的标“未覆盖”。场景描述的是必须成立的行为，不记录某次运行的结果；实测记录按协作规则放在私有仓库 `maestro-site`（重写期间的在冻结的 `records/`）。

平台规则不依靠真实模型的偶然输出：端到端闭环优先用 Oracle 执行参考解法。测试夹具选择无需模型、无付费外部依赖的任务，显式设置 oracle；Oracle 结果标为测试模式。真实模型只做少量冒烟，验证凭据、模型参数和调用接入。

UI 文案与纯样式变更不写低价值单元测试；优先验证会导致重复执行、丢结果、错计分或越权的业务行为。

## 2. 测试分层与运行方式

| 层级 | 内容 | 位置与开关 |
| --- | --- | --- |
| 单元与服务测试 | 领域规则（配置合并、摘要、状态迁移、计分与覆盖率）、仓储与并发（幂等、额度、准入竞争、共享恢复租约与等待者、pin 与回收、终态提交）、API 权限与契约、SDK | `tests/maestro/test_*.py`，默认运行。每个测试用全新的临时 data / cache / 主密钥目录（`conftest.py`），四个边界用 `src/maestro/testing/fakes.py` 的替身；Runner 协议用 `tests/maestro/fake_runner.py` 脚本化的假 Runner |
| Runner 单元 | 网络收紧、附件安装、Harness 适配 | `test_runner_*.py`、`test_fix_m5_review.py` 等；需要 `runner/` 的 uv 环境，没装时跳过 |
| 真实 Docker | Oracle 全链路、镜像在本机仓库中的保存与恢复、独立验证器与 Compose 辅助服务、取消与杀进程后的清理、网络收紧、附件在试次容器内安装 | `test_harbor_docker.py`，需 `MAESTRO_DOCKER_TESTS=1`；只用本地 fixture 任务与已有基础镜像，不调用模型和 Hub（辅助服务拉取 `ubuntu:22.04` 时会问 Docker Hub）；每个模块启动一个临时镜像仓库容器，每个测试用独立实例标签与 `<仓库>/maestro-prep/<实例>` 镜像名，结束时核对清空 |
| 真实 Hub | 目录调用、失败分类、导入与首次准备 | `test_hub_live.py`，需 `MAESTRO_HUB_TESTS=1`；匿名读取公开数据集 |
| 真实模型冒烟 | Terminus-2、OpenCode（1.x 与 2.x）、Codex、Claude Code 各一个试次 | `test_harbor_docker.py -k real_model`，需 `MAESTRO_MODEL_TESTS=1` 及模型地址、key 文件；Codex 与 Claude Code 另需各自的 Responses / Messages 地址，并在 setup 中真实安装（见交接）；会产生费用，须核对当前授权与预算 |
| 安装运维 | 安装、更新、失败回退、备份恢复、卸载与清除 | `test_install.py`，需 `MAESTRO_INSTALL_TESTS=1`；用隐藏参数 `--test-root` 把布局放进临时目录，以 systemd 用户单元运行；规则部分在 `test_maestroctl.py`、`test_backup.py`，默认运行 |
| 网页 | 关键路径冒烟 | `frontend/qa/smoke.spec.ts`（Playwright），需要全新的一次性 state 与 `serve --demo-executor` |
| CI | lint、文档链接、默认 pytest（含契约与 SDK 模型漂移）、网页 lint / `check:api` / build、第三方许可证 | `.gitea/workflows/verify.yml`，每次改了 Markdown 以外内容的推送；pytest 并行，只传 PATH 与临时 HOME。`dev` 与版本 tag 通过后发布（[12](12-operations.md) 第 1 节）。真实 Docker、Hub、模型与安装测试不在 CI，手动运行 |

隔离要求：验证使用独立 HOME、state 与主密钥，清除继承的真实凭据环境；不用已有团队 state 或真实凭据作随手测试。真实 Docker 测试前后建议对 `docker images`、`ps -a`、`network ls`、`volume ls` 留快照对比。

## 3. 需求与验收对照

| 需求编号组 | 定义位置 | 主要验收 |
| --- | --- | --- |
| DS-01～DS-09 | [02](02-datasets-preparation.md) | E2E-DS-01～05、F-21～24 |
| CFG-01～CFG-06 | [03](03-configuration-attachments.md) | E2E-RUN-01 / 04、F-11 / 19 / 25 |
| RUN-01～RUN-06 | [04](04-plans-submission.md) | E2E-RUN-01～04、F-01 / 21 / 23 / 26 |
| EXE-01～EXE-06 | [05](05-scheduling-execution.md) | F-02～03、F-05 / 08 / 22 / 24 |
| RES-01～RES-06 | [06](06-results-scoring.md) | F-04～07、F-23、E2E-OUT-01 |
| ART-01～ART-06 | [07](07-artifacts-export.md) | E2E-OUT-01、F-09～12、F-21 / 24 |
| ANA-01～ANA-06 | [08](08-collections-comparison.md) | E2E-OUT-02～03 |
| IAM-01～IAM-06 | [09](09-identity-security.md) | E2E-DS-03、E2E-RUN-04、F-13 / 25、第 8 节 |
| API-01～API-06 | [10](10-api-sdk-cli.md) | E2E-RUN-01～02、E2E-DS-04～05、F-20 |
| OPS-01～OPS-07 | [12](12-operations.md) | 12 第 9 节、F-08、F-14～15 |

网页需求由 [11](11-web-ui.md) 的页面与流程承接上述业务编号，不另造与服务端不一致的规则。

## 4. 故事一：数据集添加与准备

### E2E-DS-01 正常导入

前提：管理员网页登录，Hub 适配器可用。

步骤：搜索明确数据集 → 选择版本 → 查看作者参数与任务资源 → 添加并准备 → 等待 Operation 结束。

预期：形成不可变版本和任务目录；镜像 digest、内容摘要、首次成功准入证明可查；任务可被成员与 PAT 读取并选择；重试同一导入不生成重复版本。

覆盖：`test_datasets.py`（替身）；`test_hub_live.py`（真实 Hub）。

### E2E-DS-02 局部失败

前提：假版本含 3 个任务，控制其中 1 个构建失败。

步骤：管理员首次准备 → 成员选择全部可提交任务并预检 → 管理员单独重试首次失败项。

预期：初始目录显示 2 已准入 / 1 首次失败，缓存状态另列；预检固定 2 个任务并列出排除项；重试后 3 个均准入，旧快照仍为原 2 项。清理两个已准入缓存后，新预检仍选择这两项并提示运行时恢复。

覆盖：`test_datasets.py`。

### E2E-DS-03 SDK 发现与管理边界

前提：管理员已完成添加，提供 read PAT 和管理员创建的 experiment PAT。

步骤：SDK 读取已添加的数据集、版本 / 参数 / 准入 / 内容 / 缓存状态；尝试检索外部来源（U-20）、添加版本与手动重试首次准备。

预期：读取成功且结构化；两个 PAT 的管理请求均被拒绝。experiment PAT 可正常提交并触发运行内恢复；read PAT 不能提交，也不能以恢复接口绕过权限。

覆盖：`test_datasets.py`、`test_sdk.py`、`test_permissions.py`。

### E2E-DS-04 已准入任务的冷缓存与共享恢复

前提：管理员首次准备成功，记录准入、配置摘要及镜像 digest；回收本机准备缓存。

步骤：成员网页和 experiment PAT 各提交一个同任务运行 → 同时进入准备 → 取消其中一个等待运行 → 让另一个完成 → 按原配置重跑。

预期：两条请求均可受理，无管理员操作；同键同轮仅一个拉取 / 构建，取消不影响另一个等待者；按同一 digest 恢复且不重复预留构建容量。准入和 config_digest 不变；重跑复用缓存，每个被受理运行只扣一次额度。

覆盖：`test_preparation.py`、`test_sdk.py`（替身）；`test_harbor_docker.py`（真实镜像从本机仓库拉回同一 digest，仓库丢失后可复现的构建得到同一 digest）。

### E2E-DS-05 暂态失败与确切内容不可得

步骤：分别注入可重试下载失败、鉴权失败、原 digest 消失且无本地副本、仅取得不同摘要；再构造上游缺失但本机确切副本仍在的情形。

预期：暂态错误按预算重试，失败记录归属试次且准入保持；鉴权错误给出凭据处理入口，不标永久缺失。确切内容不可得阻止新提交与原配置重跑，返回 expected_digest；受理前拒绝不扣额度，受理后发现则形成试次错误。不同摘要不被接受，本地确切副本仍在时可以执行。

覆盖：`test_preparation.py`、`test_acceptance_p0.py`、`test_fix_prep_service.py`、`test_fix_prep_docker.py`（错误分类）。

### E2E-DS-06 独立验证器与 Compose 辅助服务

前提：本地 fixture 任务覆盖三种独立验证器镜像来源（作者给的镜像、`tests/` 下构建、照主环境再起一份），以及拉取与构建的辅助服务、验证器侧的服务、一个卷；两个任务的验证器定义相同。

步骤：导入并首次准备 → 用 Oracle 运行全部任务 → 另起一次运行，在验证阶段强杀 Runner。

预期：每项必要镜像都在首次准备中做好，相同定义只做一次；运行期间没有构建与拉取；每个容器（含独立验证器）都带平台标签，结束后容器、网络、卷清空；强杀后按标签清理也找到验证容器及其服务与卷，清理确认完成。无法准备的 Compose 写法（变量无默认值、像秘密的变量名、改变内容的构建选项、上下文越界、外部资源）首次准备失败并指出位置、不含值。

覆盖：`test_preparation_images.py`（检查、资源之和、Runner 的 override 与 Harbor 接入点）；`test_harbor_docker.py`（真实 Docker）。

## 5. 故事二：运行提交与执行

### E2E-RUN-01 Agent 独立执行完整实验

前提：管理员预先准备好目录、模型、Harness 与必要凭据；成员创建 experiment PAT。

步骤：仅使用 SDK 获取能力与目录 → 解析配置 → 检查 blockers → 幂等提交 → 轮询 Operation → 查询任务评分与日志 → 下载产物。

预期：全过程不需要网页或服务器目录；运行固定配置、字段来源和内容摘要；评分和证据可追溯；SDK 客户端超时不取消服务端运行。

覆盖：`test_e2e_run.py`、`test_sdk.py`（替身）；`test_harbor_docker.py`（真实 Oracle 从提交到产物）。

### E2E-RUN-02 断线、并发与批次

前提：PAT 仅剩 2 个受理额度。

步骤：提交包含 2 个运行的批次，模拟受理后响应丢失；使用同幂等键重试；并发尝试额外提交。

预期：只存在一个批次和两个运行，扣减 2 个额度；重试返回同一身份；额外提交被拒绝；不留下未关联试次或负额度。

覆盖：`test_batches.py`、`test_sdk.py`。

### E2E-RUN-03 取消与后续运行

前提：运行至少两个任务，控制第一个已评分、第二个执行中。

步骤：取消 → 查看已评分与取消项 → 按失败 / 未完成任务重跑 → 修改附件后再创建运行。

预期：原评分保留；重跑创建新运行且使用原快照，仅缩小任务范围；修改后运行保存差异与来源；原运行不增加试次。

覆盖：`test_reruns.py`、`test_worker.py`。

### E2E-RUN-04 成员附件的运行内安装

前提：成员或 experiment PAT 发布含 bootstrap.sh 的附件快照，数据集已准入，无管理员在线。

步骤：通过 SDK 发布附件 → 提交两个试次 → 在 setup 中安装本地测试依赖，分别注入成功、脚本错误和安装超时 → 检查网络切换、文件传输与清理。

预期：上传不执行脚本；运行受理后每试次在自己的容器安装，除本试次输出目录外无宿主目录挂载，无管理员准备请求；记录摘要、身份、目录、时限和日志。安装可按准备策略联网，执行 / 验证切换到模型服务限制；失败仅影响对应试次，无伪造零分，不污染另一容器、共享缓存或数据集准入。安装与等待均受总时限约束。

覆盖：`test_attachments.py`、`test_sdk.py`、`test_runner_attachments.py`、`test_harbor_executor.py`；`test_harbor_docker.py`（真实容器内安装、挂载与网络）。

### E2E-RUN-05 附件快照的增量发布与取回

步骤：发布一个目录 → 改一个文件，以它为父快照再发布 → 同一父快照再分出一份 → 查看差异、单个文件与整包 → 在另一目录 `fetch` 后再改 → 删除并物理删除其中一份。

预期：第二次只上传改动的文件，相同内容返回原快照；两份子快照并存，标签重复被拒绝；差异列出新增、删除与改动；`fetch` 只下载不同的文件并恢复可执行位；他人未发布的上传不能被引用；物理删除后该快照不可读，其他快照仍持有的内容保留，无人持有的内容与过期上传被清除。

覆盖：`test_attachments.py`、`test_sdk.py`、`test_migrations.py`（旧附件清空）；网页的文件夹发布、差异与下载由 `frontend/qa/smoke.spec.ts` 覆盖。

## 6. 故事三：查询与导出

### E2E-OUT-01 SDK 查询与导出

步骤：按版本、模型、时间筛选多个运行 → 批量读取 → 导出配置、摘要和产物 → 验证 manifest 与文件摘要。

预期：每份结果准确对应运行 / 任务 / 试次，零分与未评分可区分；已过期和保存失败项在清单中有显式原因；未泄露凭据。

覆盖：`test_results_export.py`、`test_sdk.py`。

### E2E-OUT-02 比较、剔除与集合

步骤：用假执行器提交几个不同模型的运行，其中一个构造限流失败和内存超限失败 → 比较 → 剔除外部原因 → 以一个运行为基准建集合 → 尝试加入资源不同的运行 → 带 `collection_id` 提交新运行与批次 → 重跑并加入 → 复制并修改规格。

预期：条件核对列出模型不同；限流被剔除并把该任务从所有列去掉，内存超限计为 0 分，运行详情页汇总不变；资源不同的运行被拒并列出字段；从集合提交的运行自动加入，不符的提交不受理；重跑是独立的一行；复制只带走符合新规格的运行；read PAT 不能修改；SDK 走同一流程得到同一张表。

覆盖：`test_comparison.py`、`test_collections.py`。

### E2E-OUT-03 快照与证据到期

步骤：保存集合的快照并写结论 → 集合加入新运行 → 把一个运行放进回收站、再物理删除 → 再查快照与集合。

预期：快照的清单、规格与数值不变，证据显示“已删除 / 已彻底删除”；集合把回收站里的运行搁置、物理删除后移除并记入修改记录；结论只有作者能改；运行未结束时不能保存快照。

覆盖：`test_collections.py`。

## 7. 必须覆盖的故障场景

| 编号 | 故障注入 | 必须满足的不变量 | 覆盖 |
| --- | --- | --- | --- |
| F-01 | 同键同内容、同键异内容并发提交 | 只受理一次，冲突稳定，额度不重复扣 | `test_acceptance.py` |
| F-02 | 两 Worker 争抢一个运行 | 一个有效租约，无并发执行 | `test_acceptance_p0.py`、`test_worker.py` |
| F-03 | 取消与结果同时提交 | 按服务端确认顺序只接受一个终态，已有评分不丢 | `test_acceptance_p0.py` |
| F-04 | 验证器输出 0、null、非法值；合法数值但 reward_valid=false | 有效 0 计分；无效数值仅作诊断，分母和覆盖率正确 | `test_scoring.py`、`test_worker.py`、`test_execution_policy.py`、`test_fix_scoring_*.py` |
| F-05 | 暂态准备失败后两次重试 | ExecutionAttempt 增加，Trial 数不变，准入不变，无恢复内层重试 | `test_preparation.py`、`test_worker.py` |
| F-06 | 提供商故障、超时、拒绝任务和未分类错误 | 按平台错误策略的 retry / grade / reward_valid（经适配器映射）；不双重重试或挑分 | `test_execution_policy.py`、`test_harbor_executor.py` |
| F-07 | 评分后上传失败 / 清理失败 | 评分保持，两个失败维度可见 | `test_acceptance_p0.py`、`test_worker.py`、`test_capacity.py` |
| F-08 | 进程在准备 / 执行 / 验证时被杀 | 中断规则一致，孤儿容器受控，未自动续跑 | `test_restart.py`、`test_worker.py`、`test_preparation.py`；`test_harbor_docker.py`（杀 Runner、杀服务后重启） |
| F-09 | 文件发布前后进程退出 | 索引 / 文件对账后无虚假 available | `test_retention.py` |
| F-10 | 磁盘到低水位 | 阻止受理 / 派发，查询和取消可用 | `test_retention.py`、`test_fix_m5_review.py` |
| F-11 | 同名版本或镜像内容漂移 | 不静默更换内容，原配置重跑拒绝或按摘要重建 | `test_datasets.py`、`test_reruns.py` |
| F-12 | 下载 / 导出与回收竞争 | pin 有限，清单准确，无无限保留 | `test_retention.py`、`test_results_export.py` |
| F-13 | 管理员 PAT、越权 owner_id、撤销 PAT | 管理接口拒绝、归属检查有效、撤销立即影响新调用 | `test_permissions.py`、`test_identity_api.py` |
| F-14 | 更新时迁移或启动失败 | 旧程序与更新前数据库一起恢复 | `test_install.py`（`MAESTRO_CTL_FAIL_AT` 注入 migrate / start） |
| F-15 | 恢复时缺所需主密钥 | 默认拒绝并说明凭据不可解密；显式接受时凭据失效，不虚报恢复完成。异机情形见 [N-21](../proposals/N-21-offhost-restore.md) | `test_backup.py` |
| F-16 | Runner stderr 洪泛、stdout 混入非协议输出、进程非零退出 | 无死锁；协议错误可定位；已完成尝试的结果保留 | `test_harbor_executor.py` |
| F-17 | Runner 协议版本或 Harbor 版本与发布清单不符 | 不把不可解析结果当正常评分 | 部分：`test_harbor_executor.py` 中运行时不符按 `RUNNER_PROTOCOL_ERROR` 结束；受理前预检未覆盖 |
| F-18 | nftables 不可用、任务 public 或已有更严网络策略 | 能力缺失拒绝；平台限制可靠收紧，不静默放宽 | `test_network_capability.py`、`test_fix_m5_review.py`；`test_harbor_docker.py`（真实网络） |
| F-19 | bootstrap 超时 / 失败、两个试次独立安装 | setup 阶段执行；不进入 Agent；无跨试次安装缓存，不污染其他试次 | `test_runner_attachments.py`；`test_harbor_docker.py` |
| F-20 | 修改 OpenAPI 后不重生成网页客户端或 SDK 类型 | CI 发现受控产物漂移并失败 | `test_sdk.py`（运行 `generate_clients.py --check`）、`test_api_contract.py`；网页类型由 CI 的 `check:api` 核对 |
| F-21 | 回收已准入缓存，成员 / PAT 提交并原配置重跑 | 均可受理；固定摘要恢复；无管理员动作；准入与配置摘要不变 | `test_acceptance_p0.py`、`test_preparation.py` |
| F-22 | 多等待者恢复、取消 / 到期与新等待者加入竞争 | 单个活动恢复租约；独立取消；发布到使用 pin 连续保护，容量不重复计 | `test_preparation.py`、`test_fix_prep_service.py` |
| F-23 | 下载超时 / 429 / 5xx、鉴权失败、原 digest 真缺失或内容不符 | 暂态不撤准入、不误标不可得；真缺失拒绝两种提交并列出摘要 | `test_acceptance_p0.py`、`test_preparation.py`、`test_hub_live.py`（失败分类） |
| F-24 | 缓存回收与发布竞争、旧租约迟到、共享构建中服务重启 | 无错误 ready；迟到不覆盖；旧试次中断，遗留构建清理，准入证明保留 | `test_preparation.py`、`test_fix_prep_service.py`、`test_fix_prep_docker.py` |
| F-25 | 成员 / PAT 的 bootstrap 请求宿主目录或越过阶段网络 | 除本试次输出目录外无宿主目录挂载；运行内安装不提升管理权限；失败与超时局限于本试次 | `test_harbor_docker.py`（挂载与网络）、`test_runner_attachments.py` |
| F-26 | 解析后、受理前管理员撤销准入 | 事务复检拒绝且不扣额度；缓存变化本身不造成解析失效 | `test_acceptance.py`、`test_acceptance_p0.py` |

## 8. 容量与安全验收

- 槽位、CPU、内存与提供商并发限制生效；放不下的运行在受理时拒绝，容器启动前不超额。覆盖：`test_capacity.py`、`test_batches.py`。
- 执行与验证阶段经 Harbor 阶段策略只访问批准范围，不能直连任意外网、宿主管理面或云元数据；主机没有收紧能力时拒绝模型运行。覆盖：`test_network_capability.py`、`test_harbor_docker.py`。
- 容器没有宿主目录（本试次输出目录除外）、Docker socket、主密钥或宿主数据库挂载；附件路径与文件传输不越界。覆盖：`test_attachments.py`、`test_harbor_docker.py`。共享构建与试次容器按同一主机总账限制 CPU / 内存：未覆盖。
- 网页、SDK、导出及日志中常见凭据的基本脱敏。覆盖：`test_fix_identity_redaction.py`、`test_security.py`、`test_sdk.py`、`test_credential_visibility.py`。脱敏测试不证明能隔离恶意代码，可信团队前提不变。
- 契约：CI 导出 OpenAPI，核对 SDK 模型与网页类型没有漂移；SDK 在未安装服务端与 Harbor 的环境中独立导入（`test_sdk.py`）；服务进程不导入 Harbor（`test_harbor_boundary.py`）。
- 最大任务规模、元数据查询目标与磁盘阈值的压测：未覆盖。

## 9. 发布检查

发布前：

- CI 通过（第 2 节）。正式版本：把代码版本改成正式版本号，合入 `master`，在 `master` 上打 `v<版本>` tag 并推送到 Gitea；CI 对 tag 再完整跑一次并发布。Gitea 上发布成功后，才把 `master` 与这个 tag 推到 GitHub（GitHub 不跑测试，U-42）。之后 `dev` 立即改成下一个 `.dev0`。
- 手动运行真实 Docker、真实 Hub 与安装运维测试并通过；模型接入另有一次真实模型冒烟（须有授权与预算）。
- 第 4～8 节中标“未覆盖”或“部分”的项目，在发布说明中写明限制。
- 未实现的能力在 `/capabilities` 的 `features` 中关闭（例如 `collections`），不靠文档宣称交付。
