# Maestro Python SDK

用一个个人访问令牌（PAT）在 Maestro 上查看数据集、解析配置、提交和重跑运行、等待结果、读取评分并下载证据。SDK 只发 HTTP 请求，不需要服务器目录、服务端代码或 Harbor；网页上能做的实验操作，SDK 都能做。数据集上传需要带 `datasets` 权限的管理员 PAT；成员、凭据、模型管理与账号设置仍在网页完成。

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

- **与服务对版本。** 服务装的是正式版本时，SDK 就是同一版本；装的是带 `unreleased` 的开发包时，安装该包随附的 SDK wheel。开发包的 SDK 与服务共用基础版本号，代码可能与同号正式版不同，不能用包索引里的同号正式 SDK 代替。

- **没有包仓库时**直接装发布附带的 wheel：`uv pip install ./maestro_sdk-<版本>-py3-none-any.whl`；在本仓库里是 `pip install ./sdk`。

开发包与已安装 SDK 可能使用同一个基础版本号，安装随附 wheel 时显式重装：`uv pip install --reinstall-package maestro-sdk ./maestro_sdk-<版本>-py3-none-any.whl`。

PAT 在网页“设置”页创建：`read` 只能读，`experiment` 还能提交、取消、重跑、发布附件和方案。令牌只显示一次，请放进文件或环境变量，不要写进代码或命令行参数。

```python
from maestro_sdk import MaestroClient

client = MaestroClient("https://maestro.example", token=open("pat.txt").read().strip())
# 或：MAESTRO_URL 与 MAESTRO_TOKEN（或 MAESTRO_TOKEN_FILE）环境变量
client = MaestroClient.from_env()
```

默认直连，不使用代理环境变量（避免令牌经过调用方没有选择的代理）；需要代理时传 `trust_env=True`。

## 在已有工具项目内评测

在项目根目录启动 Agent，沿用项目自己的构建、提交和交接方式。用项目外的 Python 环境调用 SDK，在项目内只安装两份 skill。下面以正式版服务为例，将 `<版本>` 替换为服务版本。

```bash
export MAESTRO_PYTHON="$HOME/.local/share/maestro-tools/bin/python"
uv venv "${MAESTRO_PYTHON%/bin/python}"
uv pip install --python "$MAESTRO_PYTHON" "maestro-sdk==<版本>" --index maestro=https://au12321ua.github.io/maestro/simple
"$MAESTRO_PYTHON" -c 'from maestro_sdk.skills import install; install(".")'
```

默认目标是 `.agents/skills/` 和 `.claude/skills/`。只使用一处时，例如传 `install(".", targets=[".agents/skills"])`。把 `MAESTRO_PYTHON`、`MAESTRO_URL`、`MAESTRO_TOKEN_FILE` 放进启动 Agent 的本地环境。之后由 Agent 用 `"$MAESTRO_PYTHON"` 运行 SDK 脚本。非 Python 项目也可以这样接入。升级时在同一环境安装匹配的 SDK，再执行上面的 `install(".")`。开发版服务用 `uv pip install --python "$MAESTRO_PYTHON" --reinstall-package maestro-sdk <随包 wheel 路径>`。

项目 Agent 读取这两份 skill。试次里的 Agent 使用项目构建出的工具附件。整个提交的跟踪文件都属于附件且根目录有 manifest 时，用 `attachments.publish_commit`。组件型工具先按现有构建步骤把所需文件放进干净目录，核对清单再用 `attachments.publish`。把源码修订、参与构建的未提交修改和返回的快照 ID 记在项目原有的任务记录里。[附件工作流](../skills/maestro-package/SKILL.md)

## 先问服务能做什么

```python
caps = client.require("runs", "execution")   # 版本或功能不符时抛 UnsupportedCapability
caps.limits["run_max_trials"], caps.actions["runs.submit"]
```

SDK 与服务来自同一份 OpenAPI（仓库 `openapi/maestro-v1.json`）。两边不一致时 `capabilities()` 发出 `ContractDriftWarning`，请换成与服务同版本的 SDK。

## 一次完整的实验

做多轮调试、基线对照或交接时，首次提交前先从任务或交接记录找专题链接。已有链接就读目的、最新交接和活动运行。只有名称时用 `client.topics.list(q=名称)` 查找并核对目的，有多个合理候选就请用户指明。没有合适的专题时，建议用实验问题建立专题并把链接写回原任务记录。下面假定已经选定专题。一次性试跑可直接用 `client.runs.submit`，日后用 `client.topics.change_references` 把已有运行加入专题。

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

topic = client.topics.open(topic_link)
submitted = topic.runs.submit(
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

## 用专题链接接手实验

```python
topic = client.topics.open(topic_link)
overview = topic.overview()
print(overview.topic.purpose, overview.latest_handoff)
for run in topic.runs.list(active=True, limit=25):
    print(run.id, run.state, run.scoring)
```

`client.topics.list(q="回归")` 按名称查找候选。用 `topic.references("dataset")` 查看选入的数据集，用 `topic.materials("dataset_version")` 查看运行使用的版本。确认需要后续运行时，用 `topic.runs.submit(...)`、`topic.runs.rerun(...)` 或 `topic.batches.submit(...)` 写入同一专题。交接记录可用 `client.records.create({"type": "topic", "id": topic.id}, "handoff", ...)` 写入，`topic.handoffs()` 分页读取。把 `topic.url` 给下一位即可。见[接手示例](../skills/maestro-sdk/references/topics.md)。

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
| 当前最优 | `client.collections.set_current_best(c.id, attachment_id, reason="证据支持")`；清除时传 `None`，也须给理由，不影响比较默认值 |
| 工作流记录 | `client.records.create({"type": "collection", "id": c.id}, "attachment-candidate", "候选", {"decision": "pending"}, text="给人读的 Markdown 说明", references=[{"type": "run", "id": run_id}], excerpts=[{"text": "证据摘录"}])`；`client.records.get(id)`、`list("collection", c.id, kind=...)`、`update(id, expected_revision=1, body={...})`、`history(id)`、`delete(id)`；目标也可为运行、数据集版本（`dataset_version`）或附件名（可加 `snapshot_id`），仅作者可编辑。`text` 写给人读、`body`（可省略）写给程序读，同一件事只写在一处 |
| 快照与结论 | `s = client.snapshots.create(name, collection_id=c.id)`（运行须已结束），`client.snapshots.add_note(s.id, "结论")` |
| 上传数据集 | `op = client.catalog.upload("tasks/", namespace="team", name="benchmark")`；目录下每个子目录是已转换好的 Harbor task，不另加 manifest。需要管理员的 `datasets` 权限；用 `operations.wait(op.id)` 等待导入，结果含版本与首次准备操作的标识，任务检查失败按题报告。相同内容复用版本，只传缺少的内容。来源说明用挂在数据集版本上的工作流记录保存 |
| 交付物 | `client.runs.download_deliverables(run_id, "deliverables/")` 把最终尝试的交付物按原字节保存到 `<目录>/<task_key>/<repetition>/<文件>`（路径相对于容器的 `/logs/artifacts/`），校验摘要并返回失败或过期原因。任务写入容器的 `/logs/artifacts/`；平台不定义 benchmark 提交包格式 |
| 附件 | `a = client.attachments.publish("path/to/dir", label="v2", parent=old)`（只上传服务端缺少的文件，内容相同返回原快照；跳过 `.git`），或 `client.attachments.publish_commit("path/to/repo", "HEAD", parent=old)` 发布一个 git 提交跟踪的文件（仓库根目录就是附件根目录，可执行位取自 git 的文件模式，未提交的修改不发布并给出提醒），配置里写 `attachments: [a.id]`；`client.attachments.fetch(a.id, "dir")` 只下载有差异的文件，`diff(a.id)` 与父快照比较，`download(a.id, "a.zip")` 整包下载 |
| 附件目录与回收 | `client.attachments.directory(q=...)` 按名称翻页；`client.attachments.versions(name, q=...)` 查找标签或 ID，`client.attachments.version(snapshot_id, topic_id=...)` 核对专题范围。用 `client.attachments.delete_many(ids)` 回收选定版本，逐项检查返回状态。 |
| 导出 | `e = client.exports.create([run_id])`、`client.operations.wait(e.operation_id)`、`client.exports.download(e.id, "export.zip")`。用 `client.exports.list(state="expired")` 浏览；跨页清理时先取 `selection = client.exports.expired_selection()`，核对 `selection.count` 后将这组 `selection.ids` 传给 `client.exports.delete_many`。 |

## 给 Agent 的工作区与 skill

skill 打包在 SDK 里，与 SDK 同版本，不需要服务：

```python
import maestro_sdk.skills, maestro_sdk.workspace

maestro_sdk.skills.install(".")          # 复制到 .agents/skills/ 与 .claude/skills/，记下 SDK 版本
maestro_sdk.workspace.init("my-work")    # 生成 AGENTS.md、.env.example 和更新脚本，并安装 skill
maestro_sdk.workspace.init("improve", workflow="attachments")  # 另加附件工作流说明，不覆盖已有文件
```

`maestro-sdk` 讲提交、观察、比较与诚实报告结果；`maestro-package` 讲怎样把已有材料做成附件、用实验改进它、在平台上记下当前最优的附件，以及中断后怎样只凭平台接着做。独立实验工作区采用以下布局：`AGENTS.md` 写预算、命名习惯和当前目标，`notes/` 的格式完全自由，`attachments/<名称>/` 每个附件一个 git 仓库。`init` 不覆盖已有文件，只补缺并重装 skill；升级 SDK 后再运行一次即可同步 skill。

首次配置时复制 `.env.example` 为 `.env`。在 `MAESTRO_URL` 填入服务根地址，在 `MAESTRO_TOKEN_FILE` 填入工作区外的 PAT 文件绝对路径。用 uv 加载 `.env`，更新脚本和 Agent 的 SDK 调用共用这份配置。示例中的 `codex` 可换成你的 Agent 启动命令。

```bash
# 正式版服务可先同步 SDK 和 skill
uv run --no-project --env-file .env -- .venv/bin/python -I update-maestro.py
uv run --no-project --env-file .env -- codex
```

脚本读取服务版本，从 GitHub Pages 索引选择对应的 SDK wheel，装入工作区的 `.venv`，再同步两个 skill 目录。版本匹配时直接通过。开发版服务使用[随构建提供的 wheel](#安装与连接)。

已有工作区升级 SDK 后，重跑 `workspace.init(".")` 可补齐模板。新版生成的 `.gitignore` 已包含 `/.env`。使用已有的 `.gitignore` 时，把这条规则补进去。

`maestro_sdk.workflows.attachments` 替附件改进做机械活，只组合公开 API、写本地文件，不提交运行、不判断哪个候选更好（`workflow="attachments"` 写入的 `notes/attachments-workflow.md` 有用法说明）：

```python
from maestro_sdk.workflows.attachments import (
    write_task_briefs, prepare_candidate, collect_evidence, build_report,
)

write_task_briefs(client, version.id, "briefs", tasks=["task-key"])      # 题面与公开元数据
plan = prepare_candidate(client, collection.id, parent.id, candidate.id)  # 解析但不提交
collect_evidence(client, [run.id], "evidence")       # 增量下载、清单、trace 事件索引
report = build_report(client, collection.id, "report")  # report.json 与 report.md，保留所有失败
```

## 错误

服务的错误都是 `ApiError`：`status`、`code`（稳定的错误码）、`message`、`stage`、`field_errors`（指向具体字段，如 `/repetitions`）、`retryable`、`suggested_actions`、`request_id`。`retryable` 表示同一业务请求以后可能成功，不表示可以换新幂等键再提交一次。错误和 `repr(client)` 都不含令牌。

## 维护

`models.py` 与 `_contract.py` 由 `uv run python tools/generate_clients.py` 从服务的 OpenAPI 生成，不要手改；API 改了不重新生成，测试会失败。

手写部分：`client.py` 是入口，`skills.py` 与 `workspace.py` 是不连服务的本地工具（skill 来自仓库的 `skills/`，由 `pyproject.toml` 打进 wheel 的 `maestro_sdk/_skills/`，可编辑安装时直接读仓库），`templates/` 是随 wheel 发布的工作区与工作流说明，`workflows/` 组合公开 API 完成工作流机械操作，`_transport.py` 负责认证、重试、翻页和下载，`resources/` 每组接口一个模块，`results.py` 放生成模型之外的返回类型。新增一组接口时加一个 `resources/` 模块，并在 `MaestroClient.__init__` 里挂上。
