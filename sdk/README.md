# Maestro Python SDK

用一个个人访问令牌（PAT）在 Maestro 上查看数据集、解析配置、提交和重跑运行、等待结果、读取评分并下载证据。SDK 只发 HTTP 请求，不需要服务器目录、服务端代码或 Harbor；网页上能做的实验操作，SDK 都能做（管理与账号设置除外，它们只在网页里）。

让 Agent 代为操作时，给它加载仓库里的 skill [`skills/maestro-sdk/`](../skills/maestro-sdk/SKILL.md)（`SKILL.md` 写工作流程、诚实报告结果的规则、丢失回答后的恢复，`references/` 按任务细分），本页是完整参考。

## 安装与连接

需要 Python 3.10 及以上。SDK 按版本从包仓库安装，选与服务相同的版本：

| 包仓库 | `<索引>` | 内容 |
| --- | --- | --- |
| GitHub Pages | `https://au12321ua.github.io/maestro/simple` | 自 0.4.0 起 |

```bash
uv add "maestro-sdk==<版本>" --index maestro=<索引>             # uv 项目：maestro-sdk 只从这个索引装
uv pip install "maestro-sdk==<版本>" --index maestro=<索引>     # 已有的虚拟环境
pip install "maestro-sdk==<版本>" --extra-index-url <索引>       # pip 会同时查 PyPI，务必固定版本
```

- **与服务对版本。** 服务装的是正式版本时，SDK 就是同一版本；装的是 `dev-<提交>` 预发布时，安装那个预发布附带的 wheel 文件（版本 `<版本>.dev<N>` 写在发布包 `RELEASE.json` 的 `sdk_version` 里）。

- **没有包仓库时**直接装发布附带的 wheel：`uv pip install ./maestro_sdk-<版本>-py3-none-any.whl`；在本仓库里是 `pip install ./sdk`。

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

一次逻辑提交对应一个幂等键。`submit()` 不传 `idempotency_key` 时自动生成，结果里有 `submitted.idempotency_key`。连接错误与 502 / 503 / 504 等暂时错误由 SDK 用同一个键重试，服务端只会受理一次（`submitted.replayed` 表示这是对原请求的重放）。仍然拿不到回答时抛 `TransportError`，它也带着这个键：

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
| 附件 | `a = client.attachments.publish("path/to/dir", label="v2", parent=old)`（只上传服务端缺少的文件，内容相同返回原快照；跳过 `.git`），或 `client.attachments.publish_commit("path/to/repo", "HEAD", parent=old)` 发布一个 git 提交跟踪的文件（仓库根目录就是附件根目录，可执行位取自 git 的文件模式，未提交的修改不发布并给出提醒），配置里写 `attachments: [a.id]`；`client.attachments.fetch(a.id, "dir")` 只下载有差异的文件，`diff(a.id)` 与父快照比较，`download(a.id, "a.zip")` 整包下载 |
| 导出 | `e = client.exports.create([run_id])`、`client.operations.wait(e.operation_id)`、`client.exports.download(e.id, "export.zip")` |

## 给 Agent 的工作区与 skill

skill 打包在 SDK 里，与 SDK 同版本，不需要服务：

```python
import maestro_sdk.skills, maestro_sdk.workspace

maestro_sdk.skills.install(".")          # 复制到 .agents/skills/ 与 .claude/skills/，记下 SDK 版本
maestro_sdk.workspace.init("my-work")    # AGENTS.md 模板、notes/、attachments/、.gitignore，并安装 skill
```

`maestro-sdk` 讲提交、观察、比较与诚实报告结果；`maestro-package` 讲怎样把已有材料做成附件、用实验改进它、在平台上记下当前最优的附件，以及中断后怎样只凭平台接着做。工作区是推荐布局：`AGENTS.md` 写服务地址、令牌文件位置、可花的预算与命名习惯，`notes/` 的格式完全自由，`attachments/<名称>/` 每个附件一个 git 仓库。`init` 不覆盖已有文件，只补缺并重装 skill；升级 SDK 后再运行一次即可同步 skill。

## 错误

服务的错误都是 `ApiError`：`status`、`code`（稳定的错误码）、`message`、`stage`、`field_errors`（指向具体字段，如 `/repetitions`）、`retryable`、`suggested_actions`、`request_id`。`retryable` 表示同一业务请求以后可能成功，不表示可以换新幂等键再提交一次。错误和 `repr(client)` 都不含令牌。

## 维护

`models.py` 与 `_contract.py` 由 `uv run python tools/generate_clients.py` 从服务的 OpenAPI 生成，不要手改；API 改了不重新生成，测试会失败。

手写部分：`client.py` 是入口，`skills.py` 与 `workspace.py` 是不连服务的本地工具（skill 来自仓库的 `skills/`，由 `pyproject.toml` 打进 wheel 的 `maestro_sdk/_skills/`，可编辑安装时直接读仓库），`templates/AGENTS.md` 是工作区模板，`_transport.py` 负责认证、重试、翻页和下载，`resources/` 每组接口一个模块，`results.py` 放生成模型之外的返回类型。新增一组接口时加一个 `resources/` 模块，并在 `MaestroClient.__init__` 里挂上。
