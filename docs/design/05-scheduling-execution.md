# 05 调度与执行生命周期

导航：[总览](00-overview.md) · 依赖：[提交](04-plans-submission.md)、[评分](06-results-scoring.md)、[产物](07-artifacts-export.md) · 相关代码：`src/maestro/services/worker.py`、`src/maestro/services/capacity.py`、`src/maestro/boundaries/executor.py`、`src/maestro/execution/harbor.py`、`runner/src/maestro_runner/job.py`

## 1. 拓扑与职责

服务进程内有一个 Worker，它也是唯一的调度协调器：按数据库队列领取运行、持有租约、在短事务中确认执行事件、执行取消与到期、启动时对账。SQLite 是事实来源。

调度单位是运行（[14](14-decisions.md) D-020）：一个运行由一个 Runner 子进程执行一个 Harbor Job。Runner 在独立 uv 环境中运行锁定版本的 Harbor，服务进程不导入 Harbor。试次与执行尝试仍是持久记录，状态、评分和重跑都基于它们。按试次调度见提案 [N-13](../proposals/N-13-trial-granularity.md)。

| 编号 | 要求 |
| --- | --- |
| EXE-01 | 持久队列、按受理顺序优先、重启后可恢复管理状态 |
| EXE-02 | 并发同时满足槽位、CPU、内存和提供商限制；未满足：每成员 / PAT 的活动试次限制 |
| EXE-03 | 进度与日志按准备、执行、收集、验证阶段上报 |
| EXE-04 | 取消与总时限保留已完成结果 |
| EXE-05 | 意外重启不自动恢复容器执行，明确标记中断并清理 |
| EXE-06 | 执行资源带实例 / 运行标签；未满足：试次 / 尝试级标签 |

## 2. 执行器接口

Worker 与执行器之间是进程内接口（`src/maestro/boundaries/executor.py`）：

- **Executor**：`execute(RunSpec, ExecutionSink, CancelToken)` 执行整个运行，返回资源是否确认释放及无结果试次的失败原因；`cleanup(labels)` 按标签清理；`reconcile(Leftover)` 处理上一个服务进程留下的运行；`probe` 探测本机能力（U-18）。
- **RunSpec**：全部试次（含平台准备好的本地镜像）、并发、配置快照、到期时间、标签、评分指标，以及执行时才解密的凭据值（不持久化、不写日志）。
- **ExecutionSink**：执行器只上报 Runner 身份、心跳、尝试开始、阶段、日志、产物和尝试结束（`final=False` 表示还会重试）；状态、评分判定、重试策略和租约由平台决定。Worker 的实现 **RunSink** 对每个事件开一个短写事务，先校验运行租约且运行未终结，失败即抛出 STALE_EXECUTION_LEASE，执行器随之停止 Runner 并清理。

Harbor 执行器在新进程组中启动 Runner，经 stdin 下发版本化 JSON 请求，把 stdout 的 NDJSON 事件转成 sink 调用（协议见 [15](15-stack-harbor.md) 第 5 节）。Job 的 `n_attempts` 为每任务计划试次数，`n_concurrent_trials` 为运行并发；重试在 Job 内进行，规则见 [06](06-results-scoring.md) 第 4 节。

## 3. 调度算法

Worker 循环（约 0.5 秒或被唤醒）先处理停止与到期、重试未确认的清理、推进准备协调器，再反复领取：

1. 磁盘低于派发水位时不领取，排队运行显示 STORAGE_LOW（[07](07-artifacts-export.md) 第 7 节）。
2. 按受理时间扫描排队、未请求停止的运行。预留宽度为 min(并发, 试次数)，CPU / 内存为宽度 × 各任务中最大的单试次资源，提供商名额为宽度。单试次资源是它启动的所有容器之和（主环境、独立验证器、辅助服务，[03](03-configuration-attachments.md) 第 3 节），独立验证器在 Agent 环境停止后才启动也照样计入（U-53）。
3. 与当前占用比较槽位、CPU、内存和提供商上限；放不下的写入等待原因（CAPACITY_SLOTS、CAPACITY_CPU、CAPACITY_MEMORY、PROVIDER_LIMIT），继续看下一个。第一个放得下的，在同一写事务中写入租约、service_epoch 与预留并置为 preparing。只有 Worker 领取，检查与预留同一事务完成，不会超额。
4. 占用包括持有租约的运行，以及清理尚未确认的运行：资源未确认释放就继续占容量。

允许小运行越过暂时放不下的大运行，所以不是严格 FIFO。空闲主机也放不下的运行在受理时拒绝（RUN_EXCEEDS_CAPACITY）。不提供公平调度。提供商限额只限制同时运行的试次数，不协调 RPM / TPM。容量总量在部署配置（`src/maestro/config.py`），提供商上限由管理员在网页设置，生效值见 `/api/v1/capabilities`。

### 已知限制

- 预留在领取时整体占用，等待缓存恢复期间和运行末尾并发下降时都不释放（见 N-13）。
- 缓存恢复的构建不计入主机 CPU / 内存，只受 `restore_slots` 限制。
- 没有每成员 / PAT 的活动试次限制。

## 4. 运行与试次状态

运行：queued → preparing → running → finalizing → finished / cancelled / interrupted。有试次进入执行或验证后为 running。未领取的运行被取消或到期时直接结束。finished 表示所有试次已有终态，即使全部失败；到期停止也是 finished（stop_reason=RUN_DEADLINE_EXCEEDED），用户或管理员取消为 cancelled，服务重启为 interrupted。运行的 Operation 在自然完成且有有效评分时 succeeded，否则 failed；这些状态不抹去已保存结果。

试次：queued → preparing → executing → verifying → scored / execution_error / not_started / cancelled / interrupted，reason_code 补充原因。执行器步骤到试次阶段的映射见 `STEPS`。产物与清理状态独立记录，不阻止试次终结。

状态只按允许的迁移前进，先被接受的终态生效，迟到事件被忽略。执行器表示还会重试时，当前 ExecutionAttempt 先终结，试次回到 queued（phase=retry_wait），直到成功或耗尽才写最终结果，不会先写最终错误再被重试覆盖。

## 5. 一次运行的执行顺序

1. 领取后解密凭据；不可用时运行不执行即结束（CREDENTIAL_UNAVAILABLE）。
2. 各任务并行向准备协调器申请缓存，缺失时加入按 (spec_digest, location_id) 合并的共享恢复（[02](02-datasets-preparation.md)），试次显示 restoring_cache。恢复失败为该任务每个试次记一次尝试，按 [06](06-results-scoring.md) 第 4 节重试；耗尽或不可重试的任务的试次以准备错误结束，其他任务继续。
3. 执行器核对任务材料与附件原件，缺失的试次直接以错误结束，不执行未经验证的内容。
4. Runner 中 Harbor 为每个试次创建容器、安装 Harness 与附件、收紧网络、执行 Agent、验证（[03](03-configuration-attachments.md)、[09](09-identity-security.md)）。附件在每个试次容器内独立安装，不写回共享缓存；容器只挂载执行器为本试次输出准备的目录（U-16）。
5. 每次尝试结束时，Runner 归档其目录，执行器核对摘要、脱敏后存为产物，再提交结果（[07](07-artifacts-export.md) 第 4 节）。产物保存失败不影响结果确认。
6. Runner 退出后按运行标签清理 Docker 资源；Worker 把仍无结果的试次终结，运行进入终态。

## 6. 租约与重复执行防护

每次领取生成不可复用的运行租约并记录 service_epoch。所有执行事件与结果提交都校验租约，旧租约得到 STALE_EXECUTION_LEASE。终态后到达的结果只留在尝试记录上；同一试次的结果按摘要去重，同摘要重复提交成功，不同摘要返回 RESULT_CONFLICT。

Runner 定期发心跳；stdout 静默超过 `runner_silence_sec` 即视为失联，结束进程组、按标签清理，无结果的试次记 RUNNER_UNRESPONSIVE，不创建新尝试。协议错误同样处理，记 RUNNER_PROTOCOL_ERROR。Job 内重试由 Harbor 在同一 Runner 中进行，上一次尝试的环境删除后才开始下一次。

## 7. 取消与超时

取消粒度为整个运行。未领取的运行在请求事务中直接结束。已领取的运行由 Worker 下一次循环触发取消：等待缓存时只退出自身等待，不终止其他等待者仍需的恢复；Runner 先收到 SIGTERM 并在 Harbor 内协作取消，宽限 `runner_term_grace_sec` 后 SIGKILL 进程组，再按标签清理容器、网络和出口控制 sidecar。结束进程组不能代替 Docker 清理。服务进程意外退出时 Runner 经 PDEATHSIG 收到 SIGTERM。

总时限到期按取消处理（RUN_DEADLINE_EXCEEDED）。停止后不开始新工作，已确认评分保留；执行过的未完成试次记 cancelled，未开始的记 not_started。Agent、验证和附件安装各自的超时由 Harbor 与 Runner 执行，按 [06](06-results-scoring.md) 归类。

清理无法确认时运行照常结束，cleanup_state=failed，预留继续占用容量，Worker 定期重试清理。

## 8. 意外重启与对账

启动时、开始领取之前（`Worker.start_epoch`、`Worker.recover`）：

1. 生成新的 service_epoch。
2. 上一个进程仍持有租约的未终结运行：从未有执行尝试且未请求停止的，释放租约与预留，按原受理时间留在队列；所有试次已终态的，正常结束；其余运行的在途尝试标 interrupted，执行过的未完成试次记 interrupted / SERVICE_RESTARTED，未开始的记 not_started / RUN_INTERRUPTED，不自动补跑。已确认评分保留。
3. 用持久化的 Runner 进程身份（PID、进程启动时间、boot id）确认遗留 Runner 属于本实例后才 SIGKILL 其进程组，不按可能被复用的 PID 盲目杀进程。
4. 按运行标签、再按实例标签清理遗留容器、网络和 sidecar，不触碰其他实例或无标签资源；未确认释放则 cleanup_state=failed。
5. 准备协调器对恢复操作对账（[02](02-datasets-preparation.md)），产物索引对账见 [07](07-artifacts-export.md) 第 4 节；结果写入 service_epoch 记录。

重启不会自动新建运行或消耗 PAT 额度，用户可对未完成任务创建重跑。

## 9. 停机与验收

手动停止或重启服务会中断在途运行，按第 8 节如实记录。没有带排空的维护模式（暂缓，U-48）。

关键验收：同时竞争只领取一次；满载仍不超资源；取消与评分并发保持单一终态；杀死进程后不继续原运行执行；孤儿资源按实例标签清理；产物保存失败和清理失败都不丢已确认评分。
