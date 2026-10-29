# Maestro Python SDK

用一个个人访问令牌（PAT）在 Maestro 上查看数据集、解析配置、提交和重跑运行、等待结果、读取评分并下载证据。SDK 只发 HTTP 请求，不需要服务器目录、服务端代码或 Harbor；网页上能做的实验操作，SDK 都能做（管理与账号设置除外，它们只在网页里）。

让 Agent 代为操作时，给它加载仓库里的 skill [`skills/maestro-sdk/SKILL.md`](../skills/maestro-sdk/SKILL.md)（工作流程、诚实报告结果的规则、丢失回答后的恢复），本页是完整参考。

## 安装与连接

需要 Python 3.10 及以上。在本仓库里：

```bash
pip install ./sdk          # 或 uv pip install ./sdk
```

PAT 在网页“设置”页创建：`read` 只能读，`experiment` 还能提交、取消、重跑、发布附件和方案。令牌只显示一次，请放进文件或环境变量，不要写进代码或命令行参数。

```python
from maestro_sdk import MaestroClient

client = MaestroClient("https://maestro.example", token=open("pat.txt").read().strip())
# 或：MAESTRO_URL 与 MAESTRO_TOKEN（或 MAESTRO_TOKEN_FILE）环境变量
client = MaestroClient.from_env()
```

默认直连，不使用代理环境变量（避免令牌经过调用方没有选择的代理）；需要代理时传 `trust_env=True`。

## 先问服务能做什么

```python
caps = client.require("runs", "execution")   # 版本或功能不符时抛 UnsupportedCapability
caps.limits["run_max_trials"], caps.actions["runs.submit"]
```

SDK 与服务来自同一份 OpenAPI（仓库 `openapi/maestro-v1.json`）。两边不一致时 `capabilities()` 发出 `ContractDriftWarning`，请换成与服务同版本的 SDK。

## 一次完整的实验

```python
version = next(client.catalog.datasets()).versions[0]
tasks = client.catalog.tasks(version.id)                 # 准入、内容可得性、本机缓存分开报告
model = client.configs.models()[0]
harness = client.configs.harnesses()[0]

config = {
    "dataset_version_id": version.id,
    "execution_mode": "agent",              # 或 "oracle"：任务自带参考解法，测试用
    "model_config_id": model.id,
    "harness_version_id": harness.id,
    "repetitions": 2,
}
resolution = client.configs.resolve(config).raise_for_blockers()
print(resolution.config_digest, resolution.field_sources)   # 每个生效值来自哪一层

submitted = client.runs.submit(
    name="baseline",
    resolution_id=resolution.resolution_id,
    client_request_id="round-1",            # 自己起的名字，丢了一切本地状态时用它找回
)
operation = client.operations.wait(submitted.operation_id, timeout=3600)
run = client.runs.get(submitted.run_id)
summary = client.runs.summary(run.id)
for trial in client.runs.trials(run.id):
    print(trial.task_key, trial.repetition_index, trial.state, trial.result and trial.result.score)
client.runs.download_artifacts(run.id, "evidence/")
```

- **0、未评分与失败不同。** `result.score == 0.0` 是有效零分；`score is None` 加 `state == "execution_error"` 与 `reason_code` 是没有评分；汇总里 `excluded_by_cause` 按原因计数。费用或用量未知时字段为 `None`，不是 0。
- **等待不等于取消。** `wait()` 超时抛 `WaitTimeout`，服务端的运行照常继续；要停就 `client.runs.cancel(run_id)`。
- **证据。** `download_artifacts` 只保存状态为 `available` 的文件并逐个核对摘要，其余（已过期、保存失败、已删除）在返回值里带着原因。日志可以分段读：`client.artifacts.read(artifact_id, offset=..., length=...)`。

## 提交是幂等的

一次逻辑提交对应一个幂等键。`submit()` 不传 `idempotency_key` 时自动生成，结果里有 `submitted.idempotency_key`。连接断开、5xx 等暂时错误由 SDK 用同一个键重试，服务端只会受理一次（`submitted.replayed` 表示这是对原请求的重放）。仍然拿不到回答时抛 `TransportError`，它也带着这个键：

```python
try:
    submitted = client.runs.submit(config, name="x", client_request_id="round-2")
except TransportError as lost:
    submitted = client.runs.submit(config, name="x", client_request_id="round-2",
                                   idempotency_key=lost.idempotency_key)
# 键也丢了：client.runs.find("round-2") 按 client_request_id 找回
```

有意再跑一次就用新键（不传即可）。幂等键在服务端保留 7 天。

## 其他操作

| 想做的事 | 调用 |
| --- | --- |
| 按原快照重跑全部 / 只重跑未得分的任务 / 指定任务 | `client.runs.rerun(run_id, mode="all" \| "unfinished" \| "explicit", task_ids=[...])` |
| 修改后运行 | `req = client.runs.request(run_id)`，改字段后 `client.runs.submit(req, name=..., source_run_id=run_id)` |
| 筛选运行 | `client.runs.list(dataset_version_id=..., model_config_id=..., accepted_after="2026-10-24T00:00:00Z")`，自动翻页 |
| 批次（全有或全无） | `client.batches.preview(name, [cfg1, cfg2])`、`client.batches.submit(name, [cfg1, cfg2])` |
| 实验方案 | `client.plans.create(name, template)`，提交时在配置里写 `plan_id` |
| 比较几个运行 | `client.comparisons.compare([run_a, run_b])`：条件核对、各运行汇总与逐任务对齐；外部原因的失败默认剔除，其余失败算 0 分，`exclude=[]` 不剔除 |
| 集合 | `c = client.collections.create(name, "models", base_run_id=run_id, run_ids=[...])`；`client.runs.submit(cfg, name=..., collection_id=c.id)` 按集合填好固定字段并自动加入；`client.collections.compare(c.id)`、`client.collections.candidates(c.id)`（最近不在集合里的运行，`mismatches` 为空的可加入）、`client.collections.add_runs(c.id, [run_id])` |
| 快照与结论 | `s = client.snapshots.create(name, collection_id=c.id)`（运行须已结束），`client.snapshots.add_note(s.id, "结论")` |
| 附件 | `a = client.attachments.publish("path/to/dir", label="v2", parent=old)`（只上传服务端缺少的文件，内容相同返回原快照；跳过 `.git`），配置里写 `attachments: [a.id]`；`client.attachments.fetch(a.id, "dir")` 只下载有差异的文件，`diff(a.id)` 与父快照比较，`download(a.id, "a.zip")` 整包下载 |
| 导出 | `e = client.exports.create([run_id])`、`client.operations.wait(e.operation_id)`、`client.exports.download(e.id, "export.zip")` |

## 错误

服务的错误都是 `ApiError`：`status`、`code`（稳定的错误码）、`message`、`stage`、`field_errors`（指向具体字段，如 `/repetitions`）、`retryable`、`suggested_actions`、`request_id`。`retryable` 表示同一业务请求以后可能成功，不表示可以换新幂等键再提交一次。错误和 `repr(client)` 都不含令牌。

## 维护

`models.py` 与 `_contract.py` 由 `uv run python tools/generate_clients.py` 从服务的 OpenAPI 生成，不要手改；API 改了不重新生成，测试会失败。
