# N-25 附件包的管理与改进

状态：设计中 · 优先级：待定 · 2026-10-30 · 相关：[03](../design/03-configuration-attachments.md) 第 6、7 节、[08](../design/08-collections-comparison.md)、[10](../design/10-api-sdk-cli.md)、[14](../design/14-decisions.md) U-11、U-46、U-47、U-50、U-51

## 问题

Maestro 最主要的用法之一，是让 Agent 改进一个附件包（skill 与工具）并用实验判断改动是否有效。平台已经有这件事需要的大部分对象：

- 附件快照按内容寻址，可以指明父快照（[03](../design/03-configuration-attachments.md) 第 6 节），天然是一条版本线；
- “附件 A/B”集合固定其余维度，只让附件变化，并能直接用来提交（[08](../design/08-collections-comparison.md) 第 5、6 节）；
- 分析快照冻结比较结果，结论由人或 Agent 另写（[08](../design/08-collections-comparison.md) 第 7 节）。

但 Agent 一侧没有对应的东西。Maestro 只有 SDK，没有面向 Agent 的“客户端”：怎样从已有材料做出一个合格的附件包、包的源文件和版本放在哪里、一次改进尝试要记录什么、什么时候算是一个新版本，都要 Agent 每次自己摸索。

此前在独立项目 rsi-workbench 中试过另做一个工作台：它只对接 Maestro，却要自己记账、自己维护一份附件规则、单独跟踪 SDK 与 skill 的版本，耦合很紧却分属两个项目。本提案把其中可复用的部分收进 Maestro，作为 SDK 的一部分与一个新 skill 随 Maestro 发布；rsi-workbench 不再单独开发。

## 方案

### Agent 怎样使用

用户从一个空仓库开始，装好与服务同版本的 SDK 和 skill，然后分三步交给 Agent：

1. **初始化**：“用某个本地目录 / 线上仓库的某个路径 / Maestro 上已有的附件，做一个附件包仓库。”Agent 阅读来源，决定哪些文件进入附件（只放 skill 与工具；数据、答案、凭据、大文件不进），按模板生成仓库，写 `manifest.json`，调整 `bootstrap.sh`，记录来源，检查后提交并打 `v0`。最后用一个任务做一次模型运行，从产物确认附件装上了、Agent 确实读到了 skill（Oracle 运行不能带附件，[03](../design/03-configuration-attachments.md) 第 7 节）。
2. **建立评测**：“在某数据集上改进它。”Agent 以当前版本的运行为基准，建一个“附件 A/B”集合，并在仓库里写下评测定义：集合、固定的任务列表、指标方向、重复次数、预算、晋升规则。需要留出集时，另建一个集合、写第二份评测定义；本提案不强制。
3. **改进**：Agent 反复执行“尝试”：从某个版本或尝试出发，提出有证据的假设，修改并提交，在评测上运行，逐题对照，写下结论。足够好的尝试经“晋升”成为新版本。每次尝试都留记录，失败的也留。

会话中断后，Agent 从仓库与平台推出进度：仓库里有哪些尝试已有结论，平台上按 `client_request_id` 查还有哪些运行未结束。不另存状态文件。

### 包仓库

一个仓库一个包，仓库名与附件名相同。

```
AGENTS.md                  给在本仓库工作的 Agent：规则与入口
.agents/skills/            由 SDK 安装的 maestro-sdk、maestro-package（不提交）
payload/                   == 附件根目录
  manifest.json            name 等于包名
  bootstrap.sh             把 skill/ 装到各 Harness 的 skill 目录
  skill/SKILL.md、tools/   skill 本体与工具
evaluations/<名字>.json    评测定义
journal/origin.json        来源：URL 或路径、提交、许可证、每个文件的摘要
journal/attempts/<n>.json  尝试记录（结构化）
journal/attempts/<n>.md    尝试记录（推理与证据摘录）
journal/versions/vN.json   版本记录
lessons.md                 跨尝试的经验：有效的、无效的、被否定的假设
```

`payload/` 逐字节等于附件：包工具从 **git 对象**读取文件发布，本地算出的摘要就是快照的 `content_digest`，发布后核对。payload 中只允许普通文件（git 模式 `100644`）：工具经解释器调用，`bootstrap.sh` 由平台以 `bash bootstrap.sh` 执行，都不需要可执行位，Windows 与 Linux 因而得到同一摘要。路径、manifest 字段与平台的附件检查一致。

git 引用：

| 引用 | 含义 |
| --- | --- |
| `main` | 版本主线与全部记录 |
| `attempt/<n>` 分支 | 第 n 次尝试的修改 |
| `attempt/<n>` 标签 | 第 n 次尝试实际测试的提交 |
| `vN` 标签 | 晋升的版本 |

附件的标签用 `a<n>` 与 `vN`，父快照为出发点的快照，所以平台上的父快照树与 git 中的尝试和版本一一对应。内容相同的发布返回已有快照（标签以第一次为准），记录里以快照 ID 为准。

### 尝试与晋升

尝试可以自由进行，晋升有严格的门槛。

**一次尝试**：

1. 从任一版本或已有尝试出发，读上一次的结果与 `lessons.md`。
2. 写假设，引用支持它的试次轨迹事件。
3. 在 `attempt/<n>` 分支修改 `payload/` 并提交；做本地检查。
4. 发布附件（标签 `a<n>`），从评测集合提交，`client_request_id` 为 `<包名>-a<n>-<评测名>`，重复提交不会产生第二个运行。
5. 运行结束后，按严格口径与出发点逐题对照（见下），读新的轨迹。
6. 写结论：保留（以后可以从它出发）、放弃，或提名晋升；把可复用的经验补进 `lessons.md`。

**晋升**：被提名的尝试在评测定义规定的任务与重复次数上全部完成，并按严格口径相对当前版本严格更好，才打 `vN`、写版本记录，并为该评测集合建一个分析快照，结论里写版本与提交。评测定义可以要求晋升时同时满足多个评测（例如留出集不退步）。

**严格口径**：由包工具从试次结果计算，不依赖集合比较的默认设置。

- 任务集合固定，一个不少、一个不多；每题的试次数等于规定的重复次数。
- 有效的 0 分是分数；未评分、执行失败都不算 0 分，出现时结论是“证据不足”，不剔除，也不取共同任务。
- 每题对重复取均值，再对题取宏平均，按指标方向判断。

集合比较的默认口径（剔除外部失败、未剔除的失败计 0 分、只看共同任务）服务于“并排看”，保持不变。

### 记录

两种形式都写。结构化的部分供工具与网页读取，推理写在 Markdown 里。

尝试记录 `journal/attempts/<n>.json` 至少包括：

- 出发点（版本或尝试）、测试的提交、附件快照 ID 与摘要；
- 假设列表，每条带证据引用：运行、试次、产物路径、文件摘要、定位（JSONL 行号或 ATIF 的 JSON Pointer）、事件摘要；
- 改动的文件列表与 diff 摘要，可变范围以外的改动（`manifest.json`、`bootstrap.sh`）单独列出；
- 本地检查：命令、退出码、输出摘要；
- 每个评测的运行 ID、逐题分数（未评分为 `null`）与相对出发点的变化；
- 结论：`keep`、`discard`、`nominate`，及一句理由。

`journal/attempts/<n>.md` 写观察、推理与结论，并**摘录**引用的关键事件原文。产物会按保留期被回收（[07](../design/07-artifacts-export.md) 第 2 节），摘录保证之后仍能读懂当时的依据；本提案不改保留策略。

版本记录 `journal/versions/vN.json`：来源尝试、提交、快照 ID、父版本、各评测的分数与运行、分析快照 ID。

### 交付物

都在本仓库，随 Maestro 发布：

- **SDK 中的包工具**（`maestro_sdk.package`，只用标准库和 git 命令）：生成仓库骨架；按平台规则检查 payload；从 git 提交发布附件并核对摘要；建立评测（建集合、写评测定义）；提交与找回尝试的运行；收集逐题结果与轨迹引用；严格口径对照；生成与校验尝试记录、版本记录；晋升。以 Python 函数提供，skill 中给出调用示例（不提供 CLI，U-11；见待定问题）。
- **SDK 按文件清单发布**：现在 `attachments.publish` 从目录读取，可执行位取自文件系统；增加按“路径、内容、可执行位”清单发布的入口，供从 git 发布使用。
- **skill 随 SDK 发布**：`skills/maestro-sdk/` 与新的 `skills/maestro-package/`（初始化与改进流程）作为包数据打进 SDK wheel，`maestro_sdk.skills.install(目录)` 复制到 `.agents/skills/` 与 `.claude/skills/`，并记下 SDK 版本与文件摘要。skill 与 SDK 同版本，升级 SDK 后重新安装即同步。
- **SDK 发布到 Gitea 的 Python 包仓库**：发布流程在发 Gitea Release 的同时用 `uv publish` 推送 SDK（正式版本与 `dev` 预发布），包仓库可以按版本依赖 `maestro-sdk`，不依赖本机目录。
- **包仓库模板**：`AGENTS.md`、`bootstrap.sh`、`manifest.json`、`lessons.md`、评测定义与记录的样例，放在包工具里。

### 放弃的做法

- **独立的工作台项目**：只对接 Maestro，却要另行维护附件规则、SDK 与 skill 的版本，并自己保存运行状态。
- **在服务端新增“改进线 / 轮次”对象与页面**：版本线已由附件父快照表达，比较由集合承担，结论由分析快照承担；推理记录放在包仓库，随包移动、可审阅。服务端只在后续确有需要时再补。
- **客户端账本**：为了不受产物回收影响，曾在本机保存全部产物。改为在记录中摘录关键证据；需要完整证据时用现有的导出。
- **把候选限制为“每轮一个、只能从当前版本出发”**：探索时没必要这样限制；严格要求集中放在晋升上。

## 影响

- [10](../design/10-api-sdk-cli.md)：新增包工具、按文件清单发布、skill 安装；SDK 的发布与安装方式。
- [03](../design/03-configuration-attachments.md) 第 6 节：说明“从 git 发布”和 payload 规则与平台检查的对应；平台的附件检查规则变化时，同步包工具中的规则（以契约测试保证两边一致）。
- [08](../design/08-collections-comparison.md)：说明严格口径与集合比较默认口径的分工。
- [12](../design/12-operations.md) 第 1 节与[开发流程](../guides/development-workflow.md)：发布流程增加 SDK 推送到 Gitea 包仓库。
- `skills/`：新增 `maestro-package`；`maestro-sdk` 的安装说明改为从包仓库安装。
- 数据库、服务端接口与网页不变（除非待定问题 2 选择服务端固定任务）。

## 验收

- 从一个本地 skill 目录初始化仓库：检查通过，`v0` 发布后平台的 `content_digest` 等于本地摘要；在 Windows 与 Linux 上得到同一摘要。
- 冒烟运行的产物显示 `bootstrap.sh` 成功、skill 文件已安装到所用 Harness 的目录、Agent 读取了它。
- 一次尝试从发布到记录完成；重复同一步骤不产生第二个运行或第二个快照；会话中断后能从仓库与平台恢复。
- 严格口径：某题未评分或执行失败时结论为证据不足；有效 0 分照常计入；任务集合或重复次数不符时拒绝比较。
- 晋升后 `vN` 标签、版本记录、分析快照与结论互相指向；附件父快照树与 git 中的尝试、版本一致。
- 安装的 skill 与 SDK 同版本；SDK 与服务契约不一致时给出 `ContractDriftWarning`。
- 包工具的 payload 规则与平台附件检查一致（共享测试向量，含 `files_digest`）。

## 待定问题

1. **入口形式**：包工具只提供 Python 函数（Agent 写短脚本调用），还是例外地提供 `python -m maestro_sdk.package <命令>`、以 JSON 输出（U-11 规定不提供 CLI）？后者对 Agent 更省事，也更不容易用错。
2. **固定任务列表**：集合规格现在不固定任务范围（[08](../design/08-collections-comparison.md) 第 2 节）。先只在评测定义里固定、由包工具保证，还是给集合规格增加可选的“固定任务列表”，由服务端在加入时核对？
3. **包工具的位置**：放在 `maestro-sdk` 里（一次安装），还是同一仓库中单独的 `maestro-package` 分发？
4. **SDK 包仓库的访问**：Gitea 包仓库用哪个账号与令牌推送，使用者怎样获得只读凭据；`dev` 预发布的版本号形式（如 `0.4.1.devN`）。
5. **Harness 的 skill 目录**：`bootstrap.sh` 模板要安装到哪些位置（如 `~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`、OpenCode 的目录），需按各 Harness 的上游版本实测确认。
6. **证据保留**：本提案只靠摘录。以后是否让被分析快照引用的运行产物不随保留期回收，另行讨论。
7. **已有材料迁移**：rsi-workbench 已把 scan-to-cad 的 v0–v2 迁成独立包仓库（`journal/history/` 保存旧实验记录）。本提案实施后，按新模板补齐 `AGENTS.md`、`evaluations/` 与记录格式即可，还是重新初始化？
