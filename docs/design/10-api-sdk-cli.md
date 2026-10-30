# 10 API 与 SDK 契约

导航：[总览](00-overview.md) · 依赖：[提交](04-plans-submission.md)、[权限](09-identity-security.md) · 相关代码：`src/maestro/api/`、`openapi/maestro-v1.json`、`sdk/`、`tools/generate_clients.py`

## 1. 契约原则

HTTP JSON，前缀 `/api/v1`。FastAPI 路由和 Pydantic 模型导出唯一的公共 OpenAPI，提交在 `openapi/maestro-v1.json`；网页类型（`npm run gen:api`）和 SDK 模型（`tools/generate_clients.py`）都由它生成。SDK 基于 httpx，不依赖服务端或 Harbor 代码。网页的业务动作都走公开 API，没有私有旁路；公开契约不等于 PAT 可以调用管理接口。

| 编号 | 要求 |
| --- | --- |
| API-01 | 能力、参数、默认值、约束与权限可读取 |
| API-02 | 长操作返回可持久查询的 Operation |
| API-03 | 错误包含稳定代码、阶段、字段、重试信息和下一步 |
| API-04 | 创建运行等请求支持幂等，断线不产生意外重复工作 |
| API-05 | 分页、下载与未知值语义一致；未满足：按 ID 批量查询 |
| API-06 | 版本兼容可发现，不支持的能力提前报错 |

CLI、MCP（U-11）、事件流与 webhook 都不提供；脚本用 SDK，进度靠轮询 Operation。

## 2. 响应与错误

成功响应为 `{data, meta}`，meta 含 request_id，列表另有 next_cursor。下载是二进制响应，失败时仍返回结构化错误。错误体为 `{error: {code, message, stage, field_errors[], retryable, suggested_actions[]}, meta}`，field_errors 用 JSON Pointer 指向字段。

状态码约定：202 表示异步受理并返回 Operation；400 是分页、幂等键、分段范围等格式错误；422 是请求结构（REQUEST_INVALID）或业务配置不合法；403 含 PAT 访问管理接口；404 也用于按权限不可见；409 是幂等冲突、修订过期、内容变化；410 是产物、导出包、配置解析或邀请过期；429 区分速率与 PAT 额度；503 是维护、存储不足等暂不能受理。

retryable 表示同一请求是否值得重试，不代表可以换幂等键再提交。响应不含堆栈或凭据。

## 3. 能力与健康接口

| 接口 | 内容 |
| --- | --- |
| GET /health/live | 进程可响应，无认证、无细节 |
| GET /health/ready | 数据库结构兼容即 ready，另给 accepting_runs |
| GET /api/v1/capabilities | api_version、service_version、schema_revision、contract_digest、功能开关、生效限制、团队策略修订、认证方式、actions 的权限条件 |
| GET /api/v1/system-status | 需认证：存储、结构、主密钥、执行器与 Worker 状态 |

SDK 要求 api_version 为 v1 且所需功能开启，否则抛 UNSUPPORTED_CAPABILITY；contract_digest 与 SDK 内置值不同只警告一次，没有单独的“兼容区间”。

存活、可受理、某运行可执行是三件事：ready 时 accepting_runs 仍可能为 false（存储不足、主密钥不可用、Worker 未运行），此时查看、取消和下载照常。资源实例另返回当前 allowed_actions。

## 4. 资源接口

接口的方法、字段、错误以 `openapi/maestro-v1.json` 为准，SDK 方法见 `sdk/README.md`。读取默认团队共享，写入按 [权限矩阵](09-identity-security.md) 校验。资源分组：

- 目录：能力与状态、团队策略、数据集来源 / 版本 / 任务 / 准备记录、模型配置、Harness 版本、凭据名称（U-23）、配置解析。
- 实验：附件、方案、运行（含试次、汇总、原请求、取消、重跑、删除）、批次、比较、集合（含成员、修改记录、复制）、分析快照（含结论）、产物、导出、Operation。
- 账号：初始化、会话、邀请查询与接受、本人信息、改密码、本人 PAT。
- 管理（`/api/v1/admin/`，以及 Hub 检索 `/dataset-search*`，U-20）：只接受管理员网页会话。

关键约定：

- 附件增量发布（[03](03-configuration-attachments.md) 第 6 节）：`POST /attachment-contents/missing` 问出缺哪些文件内容，逐个 `PUT /attachment-contents/{sha256}` 上传，再 `POST /attachments` 提交文件清单，同步返回快照；内容相同返回 200 与原快照。单个文件、整包 zip 与两份快照的文件差异分别在 `/attachments/{id}/files/{path}`、`/archive`、`/diff`。SDK 的 `attachments.publish(目录)`、`publish_commit(仓库, 提交)` 与 `fetch(快照, 目录)` 都只传差异的文件。
- 日志也是产物，通过 `GET /artifacts/{id}/download` 读取，可带 offset / length 分段读（单段上限 1 MiB）。已结束运行的产物可按运行、任务、试次或尝试打成 zip（`GET /runs/{id}/artifacts/archive`，[07](07-artifacts-export.md) 第 5 节），SDK 为 `runs.download_archive`。
- 修改后运行没有专门接口：`GET /runs/{id}/request` 取原请求，修改后重新解析，提交时带 source_run_id。
- 维护（更新、备份、恢复）不在 HTTP API 中（[12](12-operations.md)）。
- 比较是只计算不保存的 `POST /comparisons`；集合的比较用 `POST /collections/{id}/comparison`，请求体为空时用集合保存的设置（[08](08-collections-comparison.md)）。

### 任务准入与本机缓存响应

任务分开返回准入（admission）、内容可得性（content.availability）和本机缓存（local_preparation），不用单个 ready 字段决定能否提交：

- can_submit 只表示目录与必要配置允许选择，不代表额度、维护或容量检查通过。
- 已准入但缓存被回收的任务仍 can_submit=true，带 PREPARATION_CACHE_MISS 警告和 required_preparation，运行时自动恢复。availability=unknown 表示尚无“确切不可得”的证据。
- 首次准备未成功（TASK_NOT_ADMITTED）、必要配置缺失、显式撤销、内容不可得（RESOURCE_CONTENT_UNAVAILABLE，含 resource_kind、expected_digest 与脱敏证据）各有独立 blocker，新提交与重跑规则相同。暂态恢复失败只记 last_restore_error，准入不变。
- 配置解析返回的 required_preparation 与 cache_observations 不参与 config_digest，缓存变化不会导致 CONFIG_RESOLUTION_STALE。默认 task_selection.mode=all_eligible，受理时固定展开。
- 首次准备入口只给管理员；成员与 PAT 的恢复只由提交和重跑触发，等待者不能取消共享恢复。

## 5. 运行提交和幂等

`POST /api/v1/runs` 带 `Idempotency-Key` 头，body 为 resolution_id（或内联配置）、name、client_request_id。受理时按 resolution_id 重新解析：生效配置已变返回 409 CONFIG_RESOLUTION_STALE，解析过期返回 410 CONFIG_RESOLUTION_EXPIRED。重跑 body 含 task_selection（all / unfinished / explicit），按原快照重新解析。

- 幂等键保留 7 天，作用域为用户 + 动作 + 键，网页与 PAT 共用。同键同内容返回原受理响应，同键异内容返回 409 IDEMPOTENCY_CONFLICT；异步失败后同键仍返回原操作。
- 请求摘要取原样请求体，在方案、上限、来源运行等随时间变化的检查之前比对，重放不会因环境变化得到不同结论。
- 键过期后重复提交可能创建新运行；客户端应先按 client_request_id 查询（SDK 的 `runs.find`）。client_request_id 只用于发现，不是唯一约束。

## 6. 统一 Operation

Operation 有 kind、resource、status、phase、progress、error、result、allowed_actions、revision。status 为 accepted、running、succeeded、failed、cancelled、interrupted；failed 仍可能有部分结果，不能据此删除业务对象。重启后按业务记录重建。

取消只通过业务资源（运行、批次）发起，没有通用的 Operation.cancel，避免绕过归属检查。共享缓存恢复（kind=preparation_restore）的进度给出 spec_digest、task_key 与等待者数量；等待者只有 view，取消自己的运行即解除等待。

## 7. 查询、轮询与 SDK

列表用 created_at + id 稳定游标，游标绑定筛选条件；默认与最大 limit 见 `src/maestro/domain/limits.py`。运行列表可按提交者、状态、数据集版本、模型、Harness、附件、方案、client_request_id 和受理时间筛选。

~~~python
client = MaestroClient(service_url, token=pat)
client.require("runs", "execution")
resolved = client.configs.resolve(configuration).raise_for_blockers()
submitted = client.runs.submit(name="baseline", resolution_id=resolved.resolution_id)
client.operations.wait(submitted.operation_id, timeout=3600)
client.runs.download_artifacts(submitted.run_id, local_directory)
~~~

- 只自动重试 GET 与带幂等键的请求（网络错误与 502 / 503 / 504，遵守 Retry-After）；其他 POST 只发一次，结果未知时抛 TransportError 并带回幂等键。submit 省略幂等键时自动生成。
- `operations.wait` 从 1 秒起带抖动放慢到 5 秒；超时抛 WaitTimeout，服务端继续执行，“暂无进度”不算失败。
- 下载校验摘要；`download_artifacts` 列出跳过的过期 / 失败 / 已删产物及原因，拒绝越出目标目录的路径。
- 类型区分 None、0、未知用量与已清理产物。
- 发布附件后在本地按同一规则算出 `content_digest` 与返回的快照核对，不符抛 ContentMismatch。`publish_commit` 只读 git 对象中该提交跟踪的文件，仓库根目录就是附件根目录，可执行位取自文件模式，拒绝符号链接与子模块，工作目录有未提交的修改时警告；平台不记录提交（U-51）。

### 给 Agent 的本地工具

SDK 也是 Agent 的客户端（U-59、U-60）。平台保存别人要依赖的事实（快照、运行、比较与结论），Agent 的工作过程不规定格式，skill 只给建议。下列工具不连服务：

- `maestro_sdk.skills.install(目录)`：把随 wheel 发布的 skill（仓库的 `skills/`）复制到 `.agents/skills/` 与 `.claude/skills/`，同名 skill 整个替换、其他 skill 不动，并在 `.maestro-sdk-version` 记下 SDK 版本。skill 因此与 SDK 同版本，升级 SDK 后重装即同步。
- `maestro_sdk.workspace.init(目录)`：推荐的工作区，`AGENTS.md` 模板（服务地址、令牌文件位置、可花的预算、命名习惯、当前目标）、自由格式的 `notes/`、每个附件一个 git 仓库的 `attachments/`，以及忽略已安装 skill 与附件仓库的 `.gitignore`；已有文件不覆盖，重复调用只补缺并重装 skill。
- skill：`maestro-sdk` 讲实验操作与诚实报告；`maestro-package` 讲从已有材料做附件、用附件 A/B 集合改进、在分析快照的结论里记下当前最优的附件（不靠标签），以及中断后只凭平台恢复进度。判断“更好”时建议不剔除失败、核对每个运行的已评分试次等于计划试次，否则说明证据不足；平台不另算一套口径，集合也不核对固定任务列表（U-60）。

## 8. 契约验收

- CI（`.gitea/workflows/verify.yml`）经 `test_sdk.py` 运行 `tools/generate_clients.py --check`，并运行 `npm run check:api`，确认 SDK、网页类型与服务端不漂移；SDK 可单独安装。
- 手工构造的响应也按声明模型校验（`api/deps.py` 的 `respond`）。
- 同一业务经网页与 SDK 得到一致的快照和状态，SDK 不需要服务器文件系统。
- 断线、超时和重试不会重复受理；过期幂等键有明确提示。
- 管理接口拒绝 PAT；能力关闭或版本不兼容时明确报错。
- 已准入的冷缓存任务在网页与 SDK 均可提交；首次准备失败与内容不可得各有独立 blocker。
