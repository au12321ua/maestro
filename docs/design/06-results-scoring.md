# 06 结果状态与评分统计

导航：[总览](00-overview.md) · 依赖：[领域](01-domain-model.md)、[配置](03-configuration-attachments.md)、[执行](05-scheduling-execution.md) · 相关代码：`src/maestro/execution/policy.py`、`src/maestro/domain/scoring.py`、`src/maestro/services/results.py`

## 1. 基本原则

原始奖励、奖励有效性和评分状态分别记录。验证器实际产出的奖励存为 raw_score，没有输出时为 null。只有值是有限数值且 reward_valid=true 时，试次才是计入统计的 scored；reward_valid=false 时即使有数值也只作诊断。

有效零分是验证器真实输出的 0，不能从异常或缺失推导，缺失不补 0。Agent 超时、预算耗尽和拒绝任务仍可得到有效奖励；模型提供商错误后即使验证跑完，奖励也无效。

| 编号 | 要求 |
| --- | --- |
| RES-01 | 原始值、状态、错因、策略版本与计分口径可以追溯 |
| RES-02 | 有效零分、无评分、未执行与已清理证据明确区分 |
| RES-03 | 基础设施错误和模型 / 任务相关错误分别处理 |
| RES-04 | 自动重试与计划重复试次分别计数 |
| RES-05 | 汇总显示计划数、已评分数、排除数、未完成数和样本量 |
| RES-06 | 提供均值、标准差、通过率；不提供 pass@k |

## 2. 评分契约

运行受理时把评分契约冻结进快照，之后不变。契约包括指标 metric_id、名称、单位、方向、可选通过阈值和聚合规则（字段与校验见 `src/maestro/domain/scoring.py`）。通过只来自显式阈值，不默认“大于 0 就通过”；无阈值时不提供通过率。执行器按 metric_id 从 Harbor 奖励中取 raw_score，其余奖励作为 metrics 保存。

契约来自管理员在数据集版本页确认的版本契约；未确认时用平台默认契约（reward、越高越好、≥ 1 判通过，来源标为 platform_default），这样的版本仍可受理。确认时只接受统计代码实际能算的规则：聚合固定为“任务内已评分重复取均值、bench 取任务均值的宏平均”，其他规则拒绝，避免汇总声称用了未执行的口径。快照另记错误策略版本、聚合版本、执行器标识和团队策略修订。

### 已知限制

- 契约不含验证器版本与分数提取规则；验证器随任务内容摘要固定。
- 附件不能声明影响评分，没有由附件派生的契约。
- 不提供把某类错误计为未通过的扩展口径（count_as_failure 恒为空）。

## 3. 结果状态与错误决策表

错误策略由平台自定并带版本（U-15，`POLICY_VERSION`），初值参考 [Harbor Error classification](https://docs.harborframework.com/jobs/error-classification.md)。Harbor 适配器按锁定版本的异常类名映射到下表的 cause（`HARBOR_EXCEPTIONS`，映射标识 `MAPPING_ID`），未识别的异常为 unclassified。

| cause | reward_valid | 重试 | 继续验证 |
| --- | --- | --- | --- |
| 无错误且奖励合法 | true | — | 正常 |
| agent_exhausted_budget：超时、上下文或输出预算耗尽 | true | 否 | 是 |
| agent_refused_task：安全拒绝、内容策略拒绝 | true | 否 | 是 |
| sandbox_failed：构建、启动、OOM、环境失败 | false | 是 | 否 |
| model_provider_failed（暂态：限流、连接、服务故障） | false | 是 | 是 |
| model_provider_failed（永久：请求参数等） | false | 否 | 是 |
| model_provider_failed（认证、权限、模型不存在） | false | 否 | 否 |
| harbor_failed：框架 / 依赖能力不支持 | false | 否 | 否 |
| attachment_failed：附件安装失败、超时或原件缺失 | false | 否 | 否 |
| verifier_failed：验证超时、奖励缺失 / 为空 / 解析失败 | false | 否 | 是 |
| unclassified | false | 是 | 是 |
| cancelled | false | 否 | 否 |

reward_valid=true 的规则仍要求验证确实产出合法数值；没有异常但无可用奖励记 verifier_failed（RewardMissing）。无效奖励可以非空，所以状态不能只看 raw_score 是否为 null。“继续验证”是策略允许走验证路径，不是验证失败后反复重试。

错误记录在执行尝试上，最终结果写入 `trial_results`，都带策略版本与原始异常类型（字段见 `src/maestro/db/schema.py`）；试次 reason_code 为大写的 cause。message 已脱敏且可变，程序应依据稳定字段。平台另记管理原因，不伪装成执行器异常：USER_CANCELLED、ADMIN_CANCELLED、RUN_DEADLINE_EXCEEDED、RUN_INTERRUPTED、SERVICE_RESTARTED、RUNNER_UNRESPONSIVE、RUNNER_PROTOCOL_ERROR、CREDENTIAL_UNAVAILABLE 等。

### 平台准备错误

缓存恢复在 Harbor 启动前失败时，平台为该任务的每个等待试次记一次尝试，不运行验证，raw_score=null、reward_valid=false，最终计入排除数。

| code | 处理 |
| --- | --- |
| PREPARATION_RESTORE_FAILED：下载、拉取、构建或载入失败 | 暂态根因按第 4 节重试；配置或鉴权错误需修复后重跑；准入不变 |
| RESOURCE_CONTENT_UNAVAILABLE：必需原内容不可得 | 不重试，附 expected_digest 与证据；不撤销准入，单独更新内容可得性 |
| RESOURCE_CONTENT_MISMATCH：恢复得到不同摘要 | 不重试，不发布错误缓存，不改快照 |

受理前能发现的阻塞只返回 blocker，不创建试次、不消耗额度（[02](02-datasets-preparation.md)）。

## 4. 自动重试

可重试的失败最多再试 2 次，退避 5 秒起每次乘 4、上限 20 秒（D-011 初值，`src/maestro/execution/policy.py`），受总时限与取消约束。执行中的重试在 Harbor Job 内进行（D-020）：平台把次数、退避和由策略推出的不重试异常名单写入 RetryConfig 显式下发，不用 Harbor 默认名单，因此未列名的异常与 unclassified 一样会重试；平台不再叠加外层重试。缓存恢复失败由平台按同样次数与退避重试，共享恢复本身只试一次，加入共享操作不重置计数。

同一试次的尝试序号从 1 递增（含恢复失败记录的尝试）。最终获分只形成一个样本，失败尝试保留，汇总给出 retried_trials 与 retry_count；不从多次执行中挑最高分。提供商故障重试可能重复模型请求并增加费用，不提供金额预算或“恰好一次”保证。产物保存与清理不属于执行重试。

## 5. 单运行汇总口径

网页、API 和导出都调用 `summarize`（经 `src/maestro/services/results.py` 读取记录），同一记录得到相同计数。每个试次恰好落入一类：

| 字段 | 含义 |
| --- | --- |
| planned_trials | 计划试次数 N |
| scored_trials | 合法且 reward_valid=true 的 S |
| count_as_failure_trials | 扩展口径的 F，恒为 0 |
| excluded_error_trials | 以 execution_error 结束、不计分母的 E，另按 cause 分布 |
| unfinished_trials | 未开始、取消、中断的 U |
| active_trials / queued_trials | 尚未终结的试次 |
| passed_trials | 达到阈值的 P |
| scored_coverage / evaluation_coverage | S / N 与 (S + F) / N |
| bench_task_coverage | 本运行任务数 / 数据集版本任务数 |

终态时 N = S + F + E + U。均值只对 S 计算，无样本为 null；样本标准差用 n−1，n < 2 为 null（不返回 0 暗示稳定）；通过率为 P / (S+F)，分母为 0 或无阈值时为 null。任务级先对已评分重复取均值，部分缺失带 partial 标记；bench 分为任务均值的宏平均并给出参与任务数。网页同时展示覆盖率，不同覆盖范围的均值不能当完整排名；不同指标单位不直接求平均。

## 6. 计算例子

5 个任务各 1 次：3 个正常奖励 1、0、1；1 个 Agent 超时但验证得到有效 0；1 个环境失败。

~~~text
N=5，S=4，F=0，E=1，U=0，P=2
均值 = (1+0+1+0)/4 = 0.5，通过率 = 2/4 = 0.5，覆盖率 = 4/5 = 80%
~~~

反例：提供商错误后验证产物中出现 1，但 reward_valid=false，只供诊断，不计入 S 或 P。

## 7. 结果完整度和版本

result_completeness：所有计划试次都已评分为 complete，部分为 partial，无评分为 none；它不等于 evaluation_coverage。最终评分确认后不能改分，也没有版本化重算，需要新结果时用新运行。

用量（tokens、费用等）按 Runner 上报存于试次结果。汇总给出已知合计，以及已开始试次中有 / 无用量报告的数量，不把已知部分当成总费用。

## 8. 验收

- 表中每个 cause 都可由假执行器构造，并核对 raw_score、分母和是否重试。
- 0 分正常计入；5 个计划只有 1 个获分时，均值旁显示 20% 覆盖率。
- 三次尝试最终成功，计划试次数仍为 1，retry_count=2。
- Agent 超时和拒绝任务仍运行验证并计分；提供商错误即使有奖励数值也不计分。
- Runner 与平台不双重重试，错误映射来自锁定版本的平台策略。
- 全部排除时均值和通过率为 null；单样本标准差为 null。
