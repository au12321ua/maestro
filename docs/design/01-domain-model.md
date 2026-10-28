# 01 领域模型与数据设计

导航：[总览](00-overview.md) · 相关：[配置](03-configuration-attachments.md)、[执行](05-scheduling-execution.md)、[评分](06-results-scoring.md) · 相关代码：`src/maestro/db/schema.py`、`src/maestro/db/migrations/`、`src/maestro/domain/states.py`

## 1. 统一术语

“实验”只用于产品叙述，持久对象叫“运行”。“任务”只指数据集中的测试项目；Worker 领取的工作单元是一个运行（D-020，改变见 [N-13](../proposals/N-13-trial-granularity.md)）。

| 对象 | 身份与职责 | 可变性 |
| --- | --- | --- |
| 数据集来源 DatasetSource | 提供目录和指定版本内容；正式来源只有 Hub | 接入配置可变 |
| 数据集 Dataset | 来源 + namespace / name | 说明可更新 |
| 数据集版本 DatasetVersion | Hub 明确引用 + 解析后的上游 digest + 本地材料清单摘要 | 内容固定，保留原标签 / 修订供展示 |
| 任务 Task | 版本内的稳定任务键 | 随数据集版本固定 |
| 任务准入 TaskAdmission | 任务内容 + 已批准准备方式 + 首次成功证明 | 带修订；不随缓存回收改变 |
| 内容可得性 ContentAvailability | 必要内容摘要 + 本地 / 来源检查证据 | 可更新；暂态失败不等于不可得 |
| 准备缓存 Preparation | 实际准备规格 + 位置 + 缓存状态 | 可回收、恢复；历史由 Operation 保留 |
| 恢复等待者 PreparationWaiter | 共享恢复操作与等待者的关联；等待者是运行或管理员的首次准备 Operation | 可独立取消或到期 |
| 模型配置 ModelConfig | 提供商、模型、参数及凭据引用 | 修改产生新修订，不覆盖历史 |
| Harness 版本 HarnessVersion | 执行器适配器、内容摘要、参数契约 | 发布后固定 |
| 附件版本 AttachmentVersion | 能力包内容、作用范围及加载协议 | 发布后固定 |
| 实验方案 ExperimentPlan | 可编辑的实验模板 | 带修订号 |
| 运行 Run | 一次受理的实验；领取、租约与资源预留都在运行上 | 定义固定，状态演进 |
| 运行快照 RunSnapshot | 生效配置、来源、内容标识和口径 | 固定 |
| 试次 Trial | 运行中某任务的第几次计划执行 | 身份固定，结果演进 |
| 执行尝试 ExecutionAttempt | 同一试次的一次执行（Harbor Job 内的一次重试也是一次尝试） | 每次独立记录 |
| 工作单元 WorkItem | 每个试次一条，随运行的领取同步状态与租约；不单独领取 | 调度状态可变 |
| 提交批次 SubmissionBatch | 一次原子成组受理的一组运行 | 成员固定，状态聚合 |
| 集合 Collection | 团队维护的一组运行引用，带建立时固定的规格（[08](08-collections-comparison.md)） | 规格固定；成员与设置可变，变更有记录 |
| 分析快照 AnalysisSnapshot | 冻结的一次比较：运行清单、设置与计算结果 | 固定；结论另存，作者可改 |
| 操作记录 Operation | 一次长操作的跟踪对象 | 状态和进度可变 |

产品里的“尝试次数”叫“每任务计划试次数”，避免与 ExecutionAttempt 的自动重试混淆；API 分别用 repetitions 和 attempt_index。

## 2. 实体关系

~~~mermaid
erDiagram
    DATASET ||--o{ DATASET_VERSION : contains
    DATASET_VERSION ||--o{ TASK : defines
    TASK ||--o{ TASK_ADMISSION : admits
    TASK ||--o{ PREPARATION : prepares
    PREPARATION ||--o{ OPERATION : restores
    OPERATION ||--o{ PREPARATION_WAITER : serves
    RUN |o--o{ PREPARATION_WAITER : awaits
    OPERATION |o--o{ PREPARATION_WAITER : holds
    EXPERIMENT_PLAN o|--o{ RUN : creates
    SUBMISSION_BATCH o|--|{ RUN : groups
    RUN ||--|| RUN_SNAPSHOT : freezes
    RUN ||--|{ TRIAL : plans
    TASK ||--o{ TRIAL : evaluates
    TRIAL ||--|| WORK_ITEM : tracks
    TRIAL ||--o{ EXECUTION_ATTEMPT : executes
    TRIAL ||--o| TRIAL_RESULT : resolves
    EXECUTION_ATTEMPT ||--o{ ARTIFACT : produces
    COLLECTION }o--o{ RUN : references
    COLLECTION o|--o{ ANALYSIS_SNAPSHOT : freezes
~~~

每个运行有一个执行跟踪 Operation，批次另有聚合 Operation。首次准备、缓存恢复和导出各有自己的 Operation；同键并发恢复共享一个操作，通过 PreparationWaiter 关联多个等待者。共享恢复不归首个等待者所有，租约与进程身份单独保存，owner 为空。Operation 不代替业务对象，进度可从领域记录重建。

## 3. 通用数据约定

- 主键是服务端生成的不透明字符串；外部名称和版本号另列，不作为可变外键。
- 时间按 UTC 保存，API 使用带 Z 的 RFC 3339 字符串；网页按用户时区展示。
- CPU 用毫核，内存与磁盘用字节，时限用秒；不混用 MB 与 MiB。
- 可缺失的评分、费用、token 用 null，并给出 availability 或 reason；不用 0、-1 或空字符串代替未知。
- 参与哈希的 JSON 带 schema_version，哈希前规范化键顺序、数值和空值，不含展示文本、请求时间与凭据明文。
- 内容摘要带算法前缀，例如 `sha256:…`；目录摘要基于按路径排序的文件摘要清单，不纳入随机临时路径。
- 可修改对象带 revision；更新须提交 expected_revision，过期返回冲突。
- 除主密钥外，业务对象都可删除，走回收站两步删除（[07](07-artifacts-export.md) 第 8 节，U-08、U-19）；名义删除后已有引用仍能解析。停用与删除是两种操作，停用不影响已引用的不可变版本。

## 4. 表结构与关键约束

字段、类型与全部约束以 `src/maestro/db/schema.py` 为准，结构变更写成 `src/maestro/db/migrations/versions/` 下的 Alembic 迁移。这里只列不能从字段名看出的约束与原因。

**身份与凭据。** 邀请、会话、PAT 只存令牌摘要，可在服务端撤销；PAT 的已受理计数只能在受理事务内增加。账户删除时会话与 PAT 一并作废（U-19）。凭据只存密文与 key_version，对外仅返回引用与脱敏描述；主密钥本身不在数据库，库中只有代次与校验值（`master_key_state`）。

**名称唯一靠写事务检查，不靠唯一索引。** 数据集（来源 + namespace + name）、数据集版本（同一数据集下的 resolved_digest）、模型配置名、Harness 名称 + 版本、附件名称 + 版本，都只在“未物理删除”的记录中唯一。物理删除只留一行回执，名称可以再用，所以这些列上是普通索引，唯一性在导入或发布的写事务中检查。

**目录与准备。**

- 任务按 数据集版本 + task_key 唯一。三个摘要分开：content_digest 是上游内容身份，material_digest 是本地文件摘要，spec_digest 是准备规格摘要（缓存键）。
- 准入按 task + preparation_method_digest 唯一；证据含任务 / 输入 / 必需输出摘要、参数契约与首次成功 Operation，回收不删除。
- 内容可得性按 content_digest + resource_kind 唯一；网络或鉴权的暂态失败只记录观测，不改成 unavailable。
- 准备缓存按 spec_digest + location_id 唯一，带 generation、活动恢复与回收认领；回收认领阻止新的 pin 与发布，与准入无关。pin 按 准备 + 持有者 唯一，发布、使用、回收互斥；pin 到期须先确认资源已停止。
- 等待者按 操作 + 运行、操作 + 持有操作 唯一；取消只解除自身等待，历史记录不计入活动引用。

**运行与执行。**

- 运行的 relation 为 initial / rerun / modified；领取与租约字段（lease_token、service_epoch、reservation_json、queue_reason）都在运行上（D-020）。
- 运行快照每个运行一条，只插入不更新；同时保存 snapshot_digest、config_digest 与 comparison_digest。
- 试次按 运行 + 任务 + repetition_index 唯一；执行尝试按 试次 + attempt_index 唯一。Runner 的进程身份（PID、启动时刻、boot id、进程组）记在运行上，重启对账据此判断进程是否仍是自己启动的那个。工作单元按 trial_id 唯一，重试复用同一条。
- 试次结果每个试次至多一条。原始奖励可只供诊断；进入统计须是合法值且 reward_valid 为真。
- 产物记录只存存储键与相对路径，不返回宿主机路径。
- 幂等记录按 主体 + 动作 + key 唯一；审计事件不含令牌、密码、凭据明文。

其余表的用途：team_policies 保存团队策略修订，setup_networks 保存安装网络的版本（执行尝试的 setup_network_revision 指向其中一版），config_resolutions 保存配置解析结果（有效期内凭 resolution_id 提交），run_attachments 记录运行按顺序加载的附件版本，artifact_pins 是导出等读取者的有期限 pin，exports 是导出任务，deletions 是回收站状态与删除回执，provider_limits 是提供商并发上限，instance 与 service_epochs 是实例标识与每次启动的代次（用于租约与对账）。

## 5. 状态与派生字段

运行展示四个独立维度，取值见 `src/maestro/domain/states.py`：

| 维度 | 字段 | 含义 |
| --- | --- | --- |
| 生命周期 | state | queued → preparing → running → finalizing → finished；或 cancelled / interrupted |
| 结果完整度 | result_completeness | none / partial / complete，按已评分试次数计算 |
| 产物保存 | artifact_state | pending / saving / complete / partial / failed / expired |
| 环境清理 | cleanup_state | pending / running / complete / failed / not_required |

试次保存原始状态；运行级计数与结果完整度在查询时由试次状态计算，不缓存，也就不会有旧计数被当作受理或权限依据。scored 要求合法奖励且 reward_valid 为真；存在异常不必然否定评分，例如 Agent 超时后验证得到的奖励。运行 finished 只表示计划已终结，不意味着全部试次获分；API 另返回 completed、scored、failed、not_started 等计数。

任务可提交性由准入、固定内容可得性、必要配置及当前策略派生；本机 cache_state 不是准入条件。运行快照冻结准入 id / revision、准备方式和实际 spec_digest；缓存命中、位置和恢复进度作为运行观测另存，回收不改变 config_digest，内容检查证据与准备失败历史也不能反向改写快照。

## 6. 事务、索引与并发

必须原子的操作：

1. 受理：幂等检查、PAT 与用户有效性、任务准入修订与已知内容阻塞的复查、额度扣减、运行 / 快照 / 试次 / 工作单元 / Operation 插入。
2. 领取：一个写事务内检查运行仍可领取，写入 lease_token、service_epoch 与整体资源预留，并同步工作单元（`src/maestro/services/worker.py`）；Runner 与容器在事务外启动。
3. 结果提交：校验运行租约与尝试身份，写入最终结果并推进试次状态。
4. 取消：写入 stop_requested，阻止后续领取；已领取的通过 Runner 协议取消。
5. 恢复协调：按唯一缓存键原子认领 generation / 活动租约并添加等待者；发布时校验租约并交接 pin。取消等待者、无引用时终止恢复、回收认领都在同一记录上重检，防止取消与新加入竞争。

列表查询的索引按“过滤列 + 时间 + id”建立，以支持稳定的游标分页；具体索引见 schema。

SQLite 开启外键、WAL 与有限 busy_timeout（`src/maestro/db/engine.py`）。访问用标准 sqlite3 的同步短事务，写事务一律 `BEGIN IMMEDIATE`，先拿写锁再做“读后写”检查；HTTP 处理放线程池，每个事务独立取连接。哈希、网络请求、Docker 调用、压缩和文件复制都在事务外完成，事务内重新校验所依赖的修订。备份用 SQLite 的一致性备份接口，不只复制主数据库文件。

## 7. 保留与迁移

产物过期后保留 artifacts 的身份、摘要、大小和 expired 状态。方案或配置停用不删除快照与来源引用。任务准入和首次成功证明独立于可回收缓存保留；只有显式撤销会修改准入，内容缺失或接口不兼容通过独立阻塞表达。

结构迁移离线执行（`maestro db upgrade`，`maestroctl install / update` 会调用）。服务启动时比较数据库版本与程序的迁移头：不一致时就绪检查失败、不启动 Worker 与维护任务。不导入旧平台数据。备份与恢复见 [12](12-operations.md)。

已知限制：只有一个迁移 `0001_initial_schema`；0.x 期间不承诺兼容，兼容承诺的起点见[提案索引](../proposals/README.md) N-17。

## 8. 模块验收

- 同一受理请求重试不增加运行、试次或 PAT 计数；批次失败时不留下部分数据。
- 修改模型、方案、附件的展示名称后，历史快照内容摘要和来源关系保持稳定。
- 两个领取者竞争同一运行，只有一个取得有效租约；迟到结果不能覆盖已确认结果。
- 未评分为 null，有效零分为数值 0；数据库和导出都可区分。
- 回收不改变准入记录；并发恢复只有一个有效租约，等待者取消独立，旧租约不能发布；受理与准入撤销竞争时不误扣额度。
